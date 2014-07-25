from django.conf.urls import patterns, url, include

urlpatterns = patterns('taggit.views',
    url(r'^list$', 'list_tags', name='taggit-list'),
)


