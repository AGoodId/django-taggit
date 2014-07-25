from django.conf.urls import patterns, url

urlpatterns = patterns('taggit.views',
    url(r'^list$', 'list_tags', name='taggit-list'),
)
