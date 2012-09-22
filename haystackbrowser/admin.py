from django.core.paginator import Paginator, InvalidPage
from django.utils.encoding import force_unicode
from django.utils.translation import ugettext_lazy as _
from django.http import Http404, HttpResponseRedirect
from django.db import models
from django.utils.functional import update_wrapper
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.contrib import admin
from django.contrib.admin.views.main import PAGE_VAR, ALL_VAR, SEARCH_VAR
from django.conf import settings
from django.core.management.commands.diffsettings import module_to_dict
from haystack.query import SearchQuerySet
from haystack.forms import model_choices
from haystackbrowser.models import HaystackResults, SearchResultWrapper
from haystackbrowser.forms import PreSelectedModelSearchForm

try:
    from haystack.constants import DJANGO_CT, DJANGO_ID
except ImportError:
    DJANGO_CT = 'django_ct'
    DJANGO_ID = 'django_id'


def get_query_string(query_params, new_params=None, remove=None):
    if new_params is None:
        new_params = {}
    if remove is None:
        remove = []
    params = query_params.copy()
    for r in remove:
        for k in list(params):
            if k.startswith(r):
                del params[k]
    for k, v in new_params.items():
        if v is None:
            if k in params:
                del params[k]
        else:
            params[k] = v
    return '?%s' % params.urlencode()


class HaystackResultsAdmin(object):
    fields = None
    fieldsets = None
    exclude = None
    date_hierarchy = None
    ordering = None
    list_select_related = False
    save_as = False
    save_on_top = False

    def __init__(self, model, admin_site):
        self.model = model
        self.opts = model._meta
        self.admin_site = admin_site

    def get_model_perms(self, request):
        return {
            'add': self.has_add_permission(request),
            'change': self.has_change_permission(request),
            'delete': self.has_delete_permission(request)
        }

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    def urls(self):
        from django.conf.urls.defaults import patterns, url
        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)
        return patterns('',
            url(regex=r'^(?P<content_type>.+)/(?P<pk>.+)/$',
                view=self.view,
                name='%s_%s_change' % (self.model._meta.app_label,
                    self.model._meta.module_name)
            ),
            url(regex=r'^$',
                view=self.index,
                name='%s_%s_changelist' % (self.model._meta.app_label,
                    self.model._meta.module_name)
            ),
        )
    urls = property(urls)

    def get_results_per_page(self, request):
        return getattr(settings, 'HAYSTACK_SEARCH_RESULTS_PER_PAGE', 20)

    def get_paginator_var(self, request):
        return PAGE_VAR

    def get_search_var(self, request):
        return SEARCH_VAR

    def get_all_results_var(self, request):
        return ALL_VAR

    def get_searchresult_wrapper(self):
        return SearchResultWrapper

    def get_wrapped_search_results(self, object_list):
        klass = self.get_searchresult_wrapper()
        return [klass(x, self.admin_site.name) for x in object_list]

    def get_current_query_string(self, request):
        return get_query_string(request.GET, remove=['p'])

    def get_settings(self):
        filtered_settings = {}
        searching_for = u'HAYSTACK_'
        all_settings = module_to_dict(settings._wrapped)
        for setting_name, setting_value in all_settings.items():

            if setting_name.startswith(searching_for):
                setting_name = setting_name.replace(searching_for, '').replace('_', ' ')
                filtered_settings[setting_name] = setting_value
        return filtered_settings

    def index(self, request):
        page_var = self.get_paginator_var(request)
        form = PreSelectedModelSearchForm(request.GET or None, load_all=False)

        # Make sure there are some models indexed
        available_models = model_choices()
        if len(available_models) < 0:
            raise Http404

        # We've not selected any models, so we're going to redirect and select
        # all of them. This will bite me in the ass if someone searches for a string
        # but no models, but I don't know WTF they'd expect to return, anyway.
        # Note that I'm only doing this to sidestep this issue:
        # https://gist.github.com/3766607
        if 'models' not in request.GET.keys():
            find_all_models = ['&models=%s' % x[0] for x in available_models]
            find_all_models = ''.join(find_all_models)
            return HttpResponseRedirect('%s?%s' % (request.path_info, find_all_models))

        try:
            sqs = form.search()
            page_no = int(request.GET.get(page_var, 1))
            results_per_page = self.get_results_per_page(request)
            paginator = Paginator(sqs, results_per_page)
            page = paginator.page(page_no)
        except (InvalidPage, ValueError):
            # paginator.page may raise InvalidPage if we've gone too far
            # meanwhile, casting the querystring parameter may raise ValueError
            # if it's None, or '', or other silly input.
            raise Http404
        context = {
            'results': self.get_wrapped_search_results(page.object_list),
            'pagination_required': page.has_other_pages(),
            'page_range': paginator.page_range,
            'page_num': page.number,
            'result_count': paginator.count,
            'opts': self.model._meta,
            'title': self.model._meta.verbose_name_plural,
            'root_path': self.admin_site.root_path,
            'app_label': self.model._meta.app_label,
            'filtered': True,
            'form': form,
            'params': request.GET.items(),
            'query_string': self.get_current_query_string(request),
            'search_var': self.get_search_var(request),
            'page_var': page_var,
            'module_name': force_unicode(self.model._meta.verbose_name_plural),
        }
        return render_to_response('admin/haystackbrowser/result_list.html', context,
            context_instance=RequestContext(request))

    def view(self, request, content_type, pk):
        query = {DJANGO_ID: pk, DJANGO_CT: content_type}
        try:
            sqs = self.get_wrapped_search_results(SearchQuerySet().filter(**query)[:1])[0]
        except IndexError:
            raise Http404
        context = {
            'original': sqs,
            'title': _('View stored data for this %s') % force_unicode(sqs.verbose_name),
            'app_label': self.model._meta.app_label,
            'module_name': force_unicode(self.model._meta.verbose_name_plural),
            'haystack_settings': self.get_settings(),
            'has_change_permission': self.has_change_permission(request, sqs)
        }
        return render_to_response('admin/haystackbrowser/view.html', context,
            context_instance=RequestContext(request))
admin.site.register(HaystackResults, HaystackResultsAdmin)
