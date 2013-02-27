import logging
import re
import urllib
from urlparse import urljoin
from itertools import dropwhile
import datetime

from webhelpers.html import literal
from webhelpers.text import truncate
from pylons import tmpl_context as c, config

from ckan.lib.helpers import icon, json
import ckan.lib.helpers
from ckan.controllers.package import search_url, url_with_params
from publisher_node import PublisherNode
from ckanext.dgu.lib import formats

log = logging.getLogger(__name__)

def _is_additional_resource(resource):
    """
    Returns true iff the given resource identifies as an additional resource.
    """
    return resource.get('resource_type', '') in ('documentation',)

def _is_timeseries_resource(resource):
    """
    Returns true iff the given resource identifies as a timeseries resource.
    """
    return not _is_additional_resource(resource) and \
           resource.get('date', None)

def _is_individual_resource(resource):
    """
    Returns true iff the given resource identifies as an individual resource.
    """
    return not _is_additional_resource(resource) and \
           not _is_timeseries_resource(resource)

def additional_resources(package):
    """Extract the additional resources from a package"""
    return filter(_is_additional_resource, package.get('resources'))

def timeseries_resources(package):
    """Extract the timeseries resources from a package"""
    return filter(_is_timeseries_resource, package.get('resources'))

def individual_resources(package):
    """Extract the individual resources from a package"""
    return filter(_is_individual_resource, package.get('resources'))

def resource_type(resource):
    """
    Returns the resource type as a string.

    Returns one of 'additional', 'timeseries', 'individual'.
    """
    fs = zip(('additional', 'timeseries', 'individual'),
             (_is_additional_resource, _is_timeseries_resource, _is_individual_resource))
    return dropwhile(lambda (_,f): not f(resource), fs).next()[0]

def construct_publisher_tree(groups,  type='publisher', title_for_group=lambda x:x.title):
    """
        Uses the provided groups to generate a tree structure (in a dict) by
        matching up the tree relationship using the Member objects.

        We might look at using postgres CTE to build the entire tree inside
        postgres but for now this is adequate for our needs.
    """
    from ckan import model

    root = PublisherNode( "root", "root")
    tree = { root.slug : root }

    # Get all the member objects between groups.
    # For each member:
    #    .group_id is the group
    #    .table_id is the parent group
    members = model.Session.query(model.Member).\
                join(model.Group, model.Member.group_id == model.Group.id).\
                filter(model.Group.type == 'publisher').\
                filter(model.Member.table_name == 'group').\
                filter(model.Member.state == 'active').all()

    group_lookup  = dict( (g.id,g, ) for g in groups )
    group_members = dict( (g.id,[],) for g in groups )

    # Process the membership rules
    for member in members:
        if member.table_id in group_lookup and member.group_id:
            group_members[member.table_id].append( member.group_id )

    def get_groups(group):
        return [group_lookup[i] for i in group_members[group.id]]

    for group in groups:
        slug, title = group.name, title_for_group(group)
        if not slug in tree:
            tree[slug] = PublisherNode(slug, title)
        else:
            # May be updating a parent placeholder where the child was
            # encountered first.
            tree[slug].slug = slug
            tree[slug].title = title

        parent_nodes = get_groups(group)
        if len(parent_nodes) == 0:
            root.children.append( tree[slug] )
        else:
            for parent in parent_nodes:
                parent_slug, parent_title = parent.name, parent.title
                if not parent_slug in tree:
                    # Parent doesn't yet exist, add a placeholder
                    tree[parent_slug] = PublisherNode('', '')
                tree[parent_slug].children.append(tree[slug])
    return root

def render_tree(groups,  type='publisher'):
    return construct_publisher_tree(groups,type).render()

def render_mini_tree(all_groups,this_group):
    '''Render a tree, but a special case, where there is one 'parent' (optional),
    the current group and any number of subgroups under it.'''
    from ckan import model
    import ckanext.dgu.lib.publisher as publisher

    def get_root_group(group, critical_path):
        critical_path.insert(0, group)
        parent = publisher.get_parents(group)
        if parent:
            return get_root_group(parent[0],critical_path)
        return group, critical_path

    root_group, critical_path = get_root_group(this_group, [])
    def title_for_group(group):
        if group==this_group:
            return '<strong>%s</strong>' % group.title
        return group.title

    root = construct_publisher_tree(all_groups,'publisher',title_for_group)
    root.children = filter( lambda x: x.slug==root_group.name , root.children )

    return root.render()

def get_resource_wms(resource_dict):
    '''For a given resource, return the WMS url if it is a WMS data type.'''
    # plenty of WMS resources have res['format']='' so
    # also search for WMS in the url
    url = resource_dict.get('url') or ''
    format = resource_dict.get('format') or ''
    # NB This WMS detection condition must match that in ckanext-os/ckanext/os/controller.py
    if 'wms' in url.lower() or format.lower() == 'wms':
        return url

def get_resource_wfs(resource_dict):
    '''For a given resource, return the WMS url if it is a WMS data type.'''
    wfs_service = resource_dict.get('wfs_service') or ''
    format_ = resource_dict.get('format') or ''
    # NB This WFS detection condition must match that in ckanext-os/ckanext/os/controller.py
    if wfs_service == 'ckanext_os' or format_.lower() == 'wfs':
        return urljoin(config['ckan.site_url'], '/data/wfs')

def get_wms_info(pkg_dict):
    '''For a given package, extracts all the urls and spatial extent.
    Returns (urls, extent) where:
    * urls is a list of tuples like ('url', 'http://geog.com?wms')
    * extent is a tuple of (N, W, E, S) representing max extent
    '''
    urls = []
    for r in pkg_dict.get('resources',[]):
        wms_url = get_resource_wms(r)
        if wms_url:
            urls.append(('url', wms_url))
        wfs_url = get_resource_wfs(r)
        if wfs_url:
            urls.append(('wfsurl', wfs_url))
            urls.append(('resid', r['id']))
            resname = pkg_dict['title']
            if r['description']:
                resname += ' - %s' % r['description']
            urls.append(('resname', resname.encode('utf8')))
    # Extent
    extras = pkg_dict['extras']
    extent = {'n': get_from_flat_dict(extras, 'bbox-north-lat', ''),
              'e': get_from_flat_dict(extras, 'bbox-east-long', ''),
              'w': get_from_flat_dict(extras, 'bbox-west-long', ''),
              's': get_from_flat_dict(extras, 'bbox-south-lat', '')}
    extent_list = []
    for direction in 'nwes':
        if extent[direction] in (None, ''):
            extent_list = []
            break
        try:
            # check it is a number
            float(extent[direction])
        except ValueError, e:
            log.error('Package %r has invalid bbox value %r: %s' %
                      (pkg_dict.get('name'), extent[direction], e))
        extent_list.append(extent[direction])
    return urllib.urlencode(urls), tuple(extent_list)

def get_from_flat_dict(list_of_dicts, key, default=None):
    '''Extract data from a list of dicts with keys 'key' and 'value'
    e.g. pkg_dict['extras'] = [{'key': 'language', 'value': '"french"'}, ... ]
    '''
    for dict_ in list_of_dicts:
        if dict_.get('key', '') == key:
            return dict_.get('value', default).strip('"')
    return default

def get_uklp_package_type(package):
    return get_from_flat_dict(package.get('extras', []), 'resource-type', '')

def is_service(package):
    res_type = get_uklp_package_type(package)
    return res_type == 'service'

# variant of core ckan method of same name
# but displays better string if no resource there
def resource_display_name(resource_dict):
    name = resource_dict.get('name')
    description = resource_dict.get('description')
    if name and name != 'None':
        return name
    elif description and description != 'None':
        return description
    else:
        noname_string = 'File'
        return '[%s] %s' % (noname_string, resource_dict['id'])

def _search_with_filter(k_search,k_replace):
    from ckan.lib.base import request
    from ckan.controllers.package import search_url
    # most search operations should reset the page counter:
    params_nopage = [(k, v) for k,v in request.params.items() if k != 'page']
    params = set(params_nopage)
    params_filtered = set()
    for (k,v) in params:
        if k==k_search: k=k_replace
        params_filtered.add((k,v))
    return search_url(params_filtered)

def search_with_subpub():
    return _search_with_filter('publisher','parent_publishers')

def search_without_subpub():
    return _search_with_filter('parent_publishers','publisher')

def predict_if_resource_will_preview(resource_dict):
    format = resource_dict.get('format')
    if not format:
        return False
    normalised_format = format.lower().split('/')[-1]
    return normalised_format in (('csv', 'xls', 'rdf+xml', 'owl+xml',
                                  'xml', 'n-triples', 'turtle', 'plain',
                                  'txt', 'atom', 'tsv', 'rss'))
    # list of formats is copied from recline js

def dgu_linked_user(user, maxlength=16):  # Overwrite h.linked_user
    from ckan import model
    from ckan.lib.base import h
    from ckanext.dgu.plugins_toolkit import c

    if user in [model.PSEUDO_USER__LOGGED_IN, model.PSEUDO_USER__VISITOR]:
        return user
    if not isinstance(user, model.User):
        user_name = unicode(user)
        user = model.User.get(user_name) or model.Session.query(model.User).filter_by(fullname=user_name).first()

    this_is_me = user and (c.user in (user.name, user.fullname))

    if not user:
        # may be in the format "NHS North Staffordshire (uid 6107 )"
        match = re.match('.*\(uid (\d+)\s?\)', user_name)
        if match:
            drupal_user_id = match.groups()[0]
            user = model.User.get('user_d%s' % drupal_user_id)

    if (c.is_an_official or this_is_me):
        # only officials and oneself can see the actual user name
        if user:
            publisher = ', '.join([group.title for group in user.get_groups('publisher')])

            display_name = '%s (%s)' % (user.fullname, publisher)
            link_text = truncate(user.fullname or user.name, length=maxlength)
            return h.link_to(link_text,
                             h.url_for(controller='user', action='read', id=user.name))
        else:
            return truncate(user_name, length=maxlength)
    else:
        # joe public just gets a link to the user's publisher(s)
        import ckan.authz
        if user:
            groups = user.get_groups('publisher')
            if groups:
                return h.literal(' '.join([h.link_to(truncate(group.title, length=maxlength),
                                                     '/publisher/%s' % group.name) \
                                         for group in groups]))
            elif ckan.authz.Authorizer().is_sysadmin(user):
                return 'System Administrator'
            else:
                return 'Staff'
        else:
            return 'Staff'

def render_datestamp(datestamp_str, format='%d/%m/%Y'):
    # e.g. '2012-06-12T17:33:02.884649' returns '12/6/2012'
    if not datestamp_str:
        return ''
    try:
        return datetime.datetime(*map(int, re.split('[^\d]', datestamp_str)[:-1])).strftime(format)
    except Exception:
        return ''

def get_cache_url(resource_dict):
    url = resource_dict.get('cache_url')
    if not url:
        return
    url = url.strip().replace('None', '')
    # strip off the domain, in case this is running in test
    # on a machine other than data.gov.uk
    return url.replace('http://data.gov.uk/', '/')

def get_stars_aggregate(dataset_id):
    '''For a dataset, of all its resources, get details of the one with the highest QA score.
    returns a dict of details including:
      {'value': 3, 'last_updated': '2012-06-15T13:20:11.699', ...}
    '''
    from ckanext.qa.reports import dataset_five_stars
    stars_dict = dataset_five_stars(dataset_id)
    return stars_dict

def mini_stars_and_caption(num_stars):
    mini_stars = num_stars * '&#9733'
    mini_stars += '&#9734' * (5-num_stars)
    captions = [
        'Unavailable or not openly licensed',
        'Unstructured data (e.g. PDF)',
        'Structured data but proprietry format (e.g. Excel)',
        'Structured data in open format (e.g. CSV)',
        'Linkable data - served at URIs (e.g. RDF)',
        'Linked data - data URIs and linked to other data (e.g. RDF)'
        ]
    return literal('%s&nbsp; %s' % (mini_stars, captions[num_stars]))

def render_stars(stars, reason, last_updated):
    if stars==0:
        stars_html = 5 * icon('star-grey')
    else:
        stars_html = stars * icon('star')

    tooltip = literal('<div class="star-rating-reason"><b>Reason: </b>%s</div>' % reason) if reason else ''
    for i in range(5,0,-1):
        classname = 'fail' if (i > stars) else ''
        tooltip += literal('<div class="star-rating-entry %s">%s</div>' % (classname, mini_stars_and_caption(i)))

    datestamp = last_updated.strftime('%d/%m/%Y')
    tooltip += literal('<div class="star-rating-last-updated"><b>Score updated: </b>%s</div>' % datestamp)

    return literal('<span class="star-rating"><span class="tooltip">%s</span><a href="http://lab.linkeddata.deri.ie/2010/star-scheme-by-example/" target="_blank">%s</a></span>' % (tooltip, stars_html))

def scraper_icon(res, alt=None):
    if not alt and 'scraped' in res and 'scraper_source' in res:
        alt = "File link has been added automatically by scraping {url} on {date}. " \
              "Powered by scraperwiki.com.".format(url=res.get('scraper_source'), date=res.get('scraped').format("%d/%m/%Y"))
    return icon('scraperwiki_small', alt=alt)

def ga_download_tracking(resource, action='download'):
    '''Google Analytics event tracking for downloading a resource.

    Values for action: download, download-cache

    c.f. Google example:
    <a href="#" onClick="_gaq.push(['_trackEvent', 'Videos', 'Play', 'Baby\'s First Birthday']);">Play</a>

    The call here is wrapped in a timeout to give the push call time to complete as some browsers
    will complete the new http call without allowing _gaq time to complete.  This *could* be resolved
    by setting a target of _blank but this forces the download (many of them remote urls) into a new
    tab/window.
    '''
    return "var that=this;_gaq.push(['_trackEvent','resource','%s','%s',0,true]);"\
           "setTimeout(function(){location.href=that.href;},200);return false;" \
           % (action, resource.get('url'))

def render_datetime(datetime_, date_format=None, with_hours=False):
    '''Render a datetime object or timestamp string as a pretty string
    (Y-m-d H:m).
    If timestamp is badly formatted, then a blank string is returned.

    This is a wrapper on the CKAN one which has an American date_format.
    '''
    if not date_format:
        date_format = '%d %b %Y'
        if with_hours:
            date_format += ' %H:%M'
    return ckan.lib.helpers.render_datetime(datetime_, date_format)

def dgu_drill_down_url(params_to_keep, added_params, alternative_url=None):
    '''Since you must not mix spatial search with other facets,
    we need to strip off "sort=spatial+desc" from the params if it
    is there.

    params_to_keep: All the params apart from 'page' or
                    'sort=spatial+desc' (it is more efficient to calculate
                    this once and pass it in than to calculate it here).
                    List of (key, value) pairs.
    added_params: Dict of params to add, for this facet option
    '''
    params = set(params_to_keep)
    params |= set(added_params.items())
    if alternative_url:
        return url_with_params(alternative_url, params)
    return search_url(params)

def render_json(json_str):
    '''Given a JSON string, list or dict, return it for display,
    being pragmatic.'''
    if not json_str:
        return ''
    try:
        obj = json.loads(json_str)
    except ValueError:
        return json_str.strip('"[]{}')
    if isinstance(obj, basestring):
        return obj
    elif isinstance(obj, list):
        return ', '.join(obj)
    elif isinstance(obj, dict):
        return ', ',join(['%s: %s' % (k, v) for k, v in obj.items()])
    # json can't be anything else

def json_list(json_str):
    '''Given a JSON list, return it for display,
    being pragmatic.'''
    if not json_str:
        return []
    try:
        obj = json.loads(json_str)
    except ValueError:
        return json_str.strip('"[]{}')
    if isinstance(obj, basestring):
        return [obj]
    elif isinstance(obj, list):
        return obj
    elif isinstance(obj, dict):
        return obj.items()
    # json can't be anything else


def dgu_resource_icon(res):
    format_string = res.get('format','').strip().lower()
    fmt = formats.Formats.match(format_string)
    icon_name = 'document'
    if fmt is not None and fmt['icon']!='':
        icon_name = fmt['icon']
    url = '/images/fugue/%s.png' % icon_name
    return ckan.lib.helpers.icon_html(url)


def name_for_uklp_type(package):
    uklp_type = get_uklp_package_type(package)
    if uklp_type:
        item_type = '%s (UK Location)' % uklp_type.capitalize()
    else:
        item_type = 'Dataset'

def updated_string(package):
    if package.get('metadata_modified') == package.get('metadata_created'):
        updated_string = 'Created'
    else:
        updated_string = 'Updated'
    return updated_string

def updated_date(package):
    if package.get('metadata_modified') == package.get('metadata_created'):
        updated_date = package.get('metadata_created')
    else:
        updated_date = package.get('metadata_modified')
    return updated_date

def package_publisher_dict(package):
    groups = package.get('groups', [])
    publishers = [ g for g in groups if g.get('type') == 'publisher' ]
    return publishers[0] if publishers else {'name':'', 'title': ''}

def formats_for_package(package):
    formats = set([res.get('format') for res in package['resources']])
    if None in formats:
        formats.remove(None)
    if '' in formats:
        formats.remove('')
    return formats

def link_subpub():
    from ckan.lib.base import request
    return request.params.get('publisher','') and not request.params.get('parent_publishers','')

def facet_params_to_keep():
    from ckan.lib.base import request
    return [(k, v) for k,v in request.params.items() if k != 'page' and not (k == 'sort' and v == 'spatial desc')]

def stars_label(stars):
    try:
        stars = int(stars)
    except ValueError:
        pass
    else:
        if stars == -1:
            return 'TBC'
        stars = mini_stars_and_caption(stars)
    return stars

def uklp_display_provider(package):
    uklps = [d for d in package.get('extras', {}) if d['key'] in ('UKLP', 'INSPIRE')]
    is_uklp = uklps[0]['value'] == '"True"' if len(uklps) else False
    if not is_uklp:
        return None

    providers = [d for d in package.get('extras', {}) if (d['key'] == 'provider')]
    return providers[0]['value'] if len(providers) else ''

def random_tags():
    import random
    from ckan.lib.base import h
    tags = h.unselected_facet_items('tags', limit=20)
    random.shuffle(tags)
    return tags

def get_resource_fields(resource, pkg_extras):
    from ckan.lib.base import h
    import re
    import datetime
    from ckanext.dgu.lib.resource_helpers import ResourceFieldNames, DisplayableFields

    field_names = ResourceFieldNames()
    field_names_display_only_if_value = ['content_type', 'content_length', 'mimetype', 'mimetype-inner', 'name']
    res_dict = dict(resource)

    field_value_map = {
        # field_name : {display info}
        'url': {'label': 'URL', 'value': h.link_to(res_dict['url'], res_dict['url'])},
        'date-updated-computed': {'label': 'Date updated', 'label_title': 'Date updated on data.gov.uk', 'value': render_datestamp(res_dict.get('revision_timestamp'))},
        'content_type': {'label': 'Content Type', 'value': ''},
        'scraper_url': {'label': 'Scraper',
            'label_title':'URL of the scraper used to obtain the data',
            'value': h.literal(h.scraper_icon(c.resource)) + h.link_to(res_dict.get('scraper_url'), 'https://scraperwiki.com/scrapers/%s' %res_dict.get('scraper_url')) if res_dict.get('scraper_url') else None},
        'scraped': {'label': 'Scrape date',
            'label_title':'The date when this data was scraped',
            'value': res_dict.get('scraped')},
        'scraper_source': {'label': 'Scrape date',
            'label_title':'The date when this data was scraped',
            'value': res_dict.get('scraper_source')},
        '': {'label': '', 'value': ''},
        '': {'label': '', 'value': ''},
        '': {'label': '', 'value': ''},
        '': {'label': '', 'value': ''},
    }

    # add in fields that only display if they have a value
    for field_name in field_names_display_only_if_value:
        if pkg_extras.get(field_name):
            field_names.add([field_name])

    # calculate displayable field values
    return  DisplayableFields(field_names, field_value_map, pkg_extras)

def get_package_fields(package, pkg_extras, dataset_type):
    import re
    from ckan.lib.base import h
    from webhelpers.text import truncate
    from ckan.lib.field_types import DateType
    from ckanext.dgu.schema import GeoCoverageType
    from ckanext.dgu.lib.resource_helpers import DatasetFieldNames, DisplayableFields

    field_names = DatasetFieldNames()
    field_names_display_only_if_value = ['date_update_future', 'precision', 'update_frequency', 'temporal_granularity', 'geographic_granularity', 'taxonomy_url'] # (mostly deprecated) extra field names, but display values anyway if the metadata is there
    if c.is_an_official:
        field_names_display_only_if_value.append('external_reference')
    # work out the dataset_type
    pkg_extras = dict(pkg_extras)
    harvest_date = harvest_guid = harvest_url = dataset_reference_date = None
    if dataset_type == 'uklp':
        field_names.add(['harvest-url', 'harvest-date', 'harvest-guid', 'bbox', 'spatial-reference-system', 'metadata-date', 'dataset-reference-date', 'frequency-of-update', 'responsible-party', 'access_constraints', 'metadata-language', 'resource-type'])
        field_names.remove(['geographic_coverage', 'mandate'])
        from ckan.logic import get_action, NotFound
        from ckan import model
        from ckanext.harvest.model import HarvestObject
        try:
            context = {'model': model, 'session': model.Session}
            harvest_source = get_action('harvest_source_for_a_dataset')(context,{'id':package.id})
            harvest_url = harvest_source['url']
        except NotFound:
            harvest_url = 'Metadata not available'
        harvest_object_id = pkg_extras.get('harvest_object_id')
        if harvest_object_id:
            harvest_object = HarvestObject.get(harvest_object_id)
            if harvest_object:
                harvest_date = harvest_object.gathered.strftime("%d/%m/%Y %H:%M")
            else:
                harvest_date = 'Metadata not available'
            harvest_guid = pkg_extras.get('guid')
            harvest_source_reference = pkg_extras.get('harvest_source_reference')
            if harvest_source_reference and harvest_source_reference != harvest_guid:
                field_names.add(['harvest_source_reference'])
        if pkg_extras.get('resource-type') == 'service':
            field_names.add(['spatial-data-service-type'])
        dataset_reference_date = ', '.join(['%s (%s)' % (DateType.db_to_form(date_dict.get('value')), date_dict.get('type')) \
                       for date_dict in json_list(pkg_extras.get('dataset-reference-date'))])
    elif dataset_type == 'ons':
        field_names.add(['national_statistic', 'categories'])
        field_names.remove(['mandate'])
        if c.is_an_official:
            field_names.add(['external_reference', 'import_source'])
    elif dataset_type == 'form':
        field_names.add_at_start('theme')
        if pkg_extras.get('theme-secondary'):
            field_names.add_after('theme', 'theme-secondary')
    if c.is_an_official:
        field_names.add(['state'])

    temporal_coverage_from = pkg_extras.get('temporal_coverage-from','').strip('"[]')
    temporal_coverage_to = pkg_extras.get('temporal_coverage-to','').strip('"[]')
    if temporal_coverage_from and temporal_coverage_to:
        temporal_coverage = '%s - %s' % \
          (DateType.db_to_form(temporal_coverage_from),
           DateType.db_to_form(temporal_coverage_to))
    elif temporal_coverage_from or temporal_coverage_to:
        temporal_coverage = DateType.db_to_form(temporal_coverage_from or \
                                                temporal_coverage_to)
    else:
        temporal_coverage = ''

    taxonomy_url = pkg_extras.get('taxonomy_url') or ''
    if taxonomy_url and taxonomy_url.startswith('http'):
        taxonomy_url = h.link_to(truncate(taxonomy_url, 70), taxonomy_url)
    primary_theme = pkg_extras.get('theme-primary') or ''
    secondary_themes = pkg_extras.get('theme-secondary')
    if secondary_themes:
        from ckan.lib.helpers import json
        try:
            # JSON for multiple values
            secondary_themes = ', '.join(json.loads(secondary_themes))
        except ValueError:
            # string for single value
            secondary_themes = str(secondary_themes)

    field_value_map = {
        # field_name : {display info}
        'state': {'label': 'State', 'value': c.pkg.state},
        'harvest-url': {'label': 'Harvest URL', 'value': harvest_url},
        'harvest-date': {'label': 'Harvest Date', 'value': harvest_date},
        'harvest-guid': {'label': 'Harvest GUID', 'value': harvest_guid},
        'bbox': {'label': 'Extent', 'value': h.literal('Latitude: %s&deg; to %s&deg; <br/> Longitude: %s&deg; to %s&deg;' % (pkg_extras.get('bbox-north-lat'), pkg_extras.get('bbox-south-lat'), pkg_extras.get('bbox-west-long'), pkg_extras.get('bbox-east-long'))) },
        'categories': {'label': 'ONS Category', 'value': pkg_extras.get('categories')},
        'date-added-computed': {'label': 'Date added to data.gov.uk', 'label_title': 'Date this record was added to data.gov.uk', 'value': c.pkg.metadata_created.strftime("%d/%m/%Y")},
        'date-updated-computed': {'label': 'Date updated on data.gov.uk', 'label_title': 'Date this record was updated on data.gov.uk', 'value': c.pkg.metadata_modified.strftime("%d/%m/%Y")},
        'date_updated': {'label': 'Date data last updated', 'value': DateType.db_to_form(pkg_extras.get('date_updated', ''))},
        'date_released': {'label': 'Date data last released', 'value': DateType.db_to_form(pkg_extras.get('date_released', ''))},
        'temporal_coverage': {'label': 'Temporal coverage', 'value': temporal_coverage},
        'geographic_coverage': {'label': 'Geographic coverage', 'value': GeoCoverageType.strip_off_binary(pkg_extras.get('geographic_coverage', ''))},
        'resource-type': {'label': 'Gemini2 resource type', 'value': pkg_extras.get('resource-type')},
        'spatial-data-service-type': {'label': 'Gemini2 service type', 'value': pkg_extras.get('spatial-data-service-type')},
        'access_constraints': {'label': 'Access constraints', 'value': render_json(pkg_extras.get('access_constraints'))},
        'taxonomy_url': {'label': 'Taxonomy URL', 'value': taxonomy_url},
        'theme': {'label': 'Theme', 'value': primary_theme},
        'theme-secondary': {'label': 'Secondary Theme(s)', 'value': secondary_themes},
        'metadata-language': {'label': 'Metadata language', 'value': pkg_extras.get('metadata-language', '').replace('eng', 'English')},
        'metadata-date': {'label': 'Metadata date', 'value': DateType.db_to_form(pkg_extras.get('metadata-date', ''))},
        'dataset-reference-date': {'label': 'Dataset reference date', 'value': dataset_reference_date},
        '': {'label': '', 'value': ''},
    }

    # add in fields that only display if they have a value
    for field_name in field_names_display_only_if_value:
        if pkg_extras.get(field_name):
            field_names.add([field_name])

    # calculate displayable field values
    return DisplayableFields(field_names, field_value_map, pkg_extras)

def results_sort_by():
    # Default to location if there is a bbox and no other parameters. Otherwise
    # relevancy if there is a keyword, otherwise popularity.
    # NB This ties in with the default sort set in ckanext/dgu/plugin.py
    from ckan.lib.base import request
    bbox = request.params.get('ext_bbox')
    search_params_apart_from_bbox_or_sort = [key for key, value in request.params.items()
                                         if key not in ('ext_bbox', 'sort') and value != '']
    return c.sort_by_fields or ('spatial' if not sort_by_location_disabled() else ('rank' if c.q else 'popularity'))

def sort_by_location_disabled():
    # TODO: Duplicated code from above, needs tidying
    from ckan.lib.base import request
    bbox = request.params.get('ext_bbox')
    search_params_apart_from_bbox_or_sort = [key for key, value in request.params.items()
                                         if key not in ('ext_bbox', 'sort') and value != '']
    return not(bool(bbox and not search_params_apart_from_bbox_or_sort))

def relevancy_disabled():
    from ckan.lib.base import request
    return not(bool(request.params.get('q')))

def get_resource_formats():
    from ckanext.dgu.lib.formats import Formats
    import json
    resource_formats = json.dumps(Formats.by_display_name().keys())

def get_wms_info_urls(pkg_dict):
    return get_wms_info(pkg_dict)[0]

def get_wms_info_extent(pkg_dict):
    return get_wms_info(pkg_dict)[1]

def get_resource_stars(resource_id):
    from ckanext.qa import reports
    report = reports.resource_five_stars(resource_id)
    stars = report.get('openness_score', -1)
    return stars

def get_star_text(resource_id):
    stars = get_resource_stars(resource_id)
    stars_text = str(stars) + "/5"
    if (stars==4):
        stars_text = "4/5 or 5/5"
    return stars_text

def star_report_reason(resource_id):
    from ckanext.qa import reports
    report = reports.resource_five_stars(resource_id)
    return report.get('openness_score_reason')

def star_report_updated(resource_id):
    from ckanext.qa import reports
    report = reports.resource_five_stars(resource_id)
    return report.get('openness_updated')

def groups_as_json(groups):
    import json
    return json.dumps([group.title for group in groups])

def user_display_name(user):
    user_str = ''
    if user.get('fullname'):
        user_str += user['fullname']
    user_str += ' [%s]' % user['name']
    return user_str

def pluralise_text(num):
    return 's' if num > 1 else ''

def group_category(group_extras):
    category = group_extras.get('category')
    if category:
        from ckanext.dgu.forms.validators import categories
        return dict(categories).get(category, category)
    return None

def spending_published_by(group_extras):
    from ckan import model
    spb = group_extras.get('spending_published_by')
    if spb:
        return model.Group.by_name(spb)
    return None

def advanced_search_url():
    from ckan.controllers.package import search_url
    from ckan.lib.base import request
    params = dict(request.params)
    if not 'publisher' in params:
        params['parent_publishers'] = c.group.name
    return search_url(params.items())

