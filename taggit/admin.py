import re
from __future__ import unicode_literals

from django.contrib import admin
from django.utils.translation import ugettext_lazy as _, ugettext
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from begood.contrib.admin.widgets import TextAutocomplete
from begood_sites.admin import SiteModelAdmin, SiteVersionAdmin
from begood_sites.models import VersionSite
import reversion

from taggit.models import Tag, TaggedItem
from taggit.widgets import TagNameWidget


class TagFilter(admin.SimpleListFilter):
  title = _('tags')
  parameter_name = 'tags'
  max_display_length = 30

  def lookups(self, request, model_admin):
    """
    Show only the tags on this site
    """
    qs = Tag.on_site.all().order_by('name')
    lookups = [(t.slug, t.namespace + ": " + unicode(t) if t.namespace else
      unicode(t)) for t in qs]
    lookups = [(l[0], l[1][:self.max_display_length-3]+'...' if len(l[1]) >
      self.max_display_length else l[1]) for l in lookups]
    return lookups

  def queryset(self, request, queryset):
    value = self.value()
    if value is None:
      return queryset
    else:
      return queryset.filter(tags__slug=unicode(value))


class NamespaceFilter(admin.SimpleListFilter):
  title = _('namespace')
  parameter_name = 'namespace'

  def lookups(self, request, model_admin):
    """
    Show only the namespaces in the queryset
    """
    qs = model_admin.queryset(request)
    namespaces = qs.values_list('namespace', flat=True).order_by('namespace').distinct()
    lookups = [(ns, ns if ns else _('None')) for ns in namespaces]
    return lookups

  def queryset(self, request, queryset):
    value = self.value()
    if value is None:
      return queryset
    else:
      return queryset.filter(namespace=unicode(value))


def tagged_items_count(obj):
    """
    Get the number of tagged items on this site
    """
    # To optimize the queries, do one query per content type and aggregate the
    # number of objects on the current site for that content type.
    count = 0
    tagged_items = obj.taggit_taggeditem_items.all()
    ctypes = tagged_items.values_list('content_type', flat=True).distinct()
    for ctype_id in ctypes:
        model_class = ContentType.objects.get(pk=ctype_id).model_class()
        if hasattr(model_class, 'sites'):
            obj_ids = tagged_items.filter(content_type=ctype_id).values_list('object_id',
                flat=True)
            rel_name = model_class.sites.field.related_query_name()
            count += model_class.sites.through.objects.filter(
                **{'site_id': settings.SITE_ID, rel_name+'_id__in': obj_ids}).count()
        else:
            count += tagged_items.filter(content_type=ctype_id).count()
    return count
tagged_items_count.short_description = _('Tagged Items Count')


def tag_name(obj):
    return unicode(obj)
tag_name.short_description = _('Tag')


class TaggedItemInline(admin.StackedInline):
    model = TaggedItem


class TagAdmin(SiteVersionAdmin, SiteModelAdmin):
    list_display = ["namespace", tag_name, tagged_items_count,]
    list_filter = [NamespaceFilter]
    search_fields = ["name",]
    fields = ['name', 'namespace', 'slug', 'sites']
    list_display_links = [tag_name]
    prepopulated_fields = {'slug': ('name',)}
    list_per_page = 50
    list_max_show_all = 10000
    change_form_template = 'admin/reversion_change_form.html'
    change_list_template = 'admin/change_list.html'

    actions = ['delete_selected']

    def delete_selected(modeladmin, request, queryset):
        sites = request.user.get_sites()
        for tag in queryset:
            modeladmin.delete_model(request, tag)
    delete_selected.short_description = _("Delete selected Tags")

    def get_list_display(self, request):
        list_display = self.list_display

        if 'all' in request.GET:
          return list_display[:2]
        else:
          return list_display

    def get_site_queryset(self, obj, user):
        return user.get_sites()

    def formfield_for_dbfield(self, field, **kwargs):
        # For the name field, use a widget that strips the namespace
        if field.name == 'name':
            kwargs['widget'] = TagNameWidget()
        # For the namespace field, use an autocomplete widget
        if field.name == 'namespace':
            choices = Tag.on_site.exclude(namespace='')\
                .values_list('namespace', flat=True)\
                .order_by('namespace').distinct()
            kwargs['widget'] = TextAutocomplete(choices=choices)
        return super(TagAdmin, self).formfield_for_dbfield(field, **kwargs)

    def delete_model(self, request, obj):
        """
        Given a model instance delete it from the database.
        """
        sites = request.user.get_sites()
        with transaction.commit_on_success():
            with reversion.create_revision(manage_manually=True):
                revision = reversion.default_revision_manager.save_revision(
                    [obj],
                    user=request.user,
                    comment=_("Deleted tag."),
                    meta=[(VersionSite, {'site': site}) for site in sites]
                    )
                obj.delete(sites)

    def save_form(self, request, form, change):
        """
        Given a model instance save it to the database.
        """
        obj = form.instance

        # Add the namespace to the name
        if obj.namespace and not ':' in obj.name:
            obj.name = obj.namespace + ':' + obj.name

        if obj.pk:
            try:
                original = Tag.objects.get(pk=obj.pk)
                if obj.name != original.name or obj.slug != original.slug:
                    # The tag has been changed. If it's on multiple sites, keep the
                    # original and create a new tag with the new name
                    new_sites = form.cleaned_data['sites']
                    obj_sites = obj.sites.all()
                    if all(s in new_sites for s in obj_sites):
                        # This tag is not on any other sites, so allow this
                        # rename as usual
                        return form.save(commit=False)
                    else:
                        # Create a new tag with the new name and slug
                        new_obj = Tag(name=obj.name, slug=obj.slug)
                        form.instance = new_obj
                        new_obj.save()

                        # Ugly, but this is the easiest way to make the redirect work
                        request.path = re.sub("/%d/" % obj.pk, "/%d/" % new_obj.pk, request.path)

                        # Switch the sites from the old to the new tag
                        for site in new_sites:
                            obj.sites.remove(site)
                            new_obj.sites.add(site)

                        # Re-tag all items belonging to the changed sites
                        tagged_items = obj.taggit_taggeditem_items.all()
                        for item in tagged_items:
                            if hasattr(item.content_object, 'sites'):
                                if item.content_object.sites.exclude(id__in=[s.id for s
                                    in new_sites]).count() == 0:
                                    item.delete()
                                if item.content_object.sites.filter(id__in=[s.id for s
                                    in new_sites]).count() > 0:
                                    item.content_object.tags.add(new_obj)

                        return new_obj
            except Tag.DoesNotExist:
                # When restoring with reversion, the tag has a pk but no
                # element with that pk exists. Just save it.
                pass
        return form.save(commit=False)

    def save_related(self, request, form, formsets, change):
        obj = form.instance
        original = obj.sites.all()
        user_sites = request.user.get_sites()
        new_sites = form.cleaned_data['sites']
        # Don't remove any sites the user doesn't have access to
        sites_to_remove = [s for s in original if s in user_sites and s not in new_sites]
        sites_to_add = [s for s in new_sites if s not in original]
        for site in sites_to_add:
            obj.sites.add(site)
        # Only remove sites when editing and not when adding tags to new sites
        if change:
            obj.delete(sites_to_remove)


admin.site.register(Tag, TagAdmin)
