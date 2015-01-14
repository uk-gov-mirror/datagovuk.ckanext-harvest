import logging
import urlparse
import json

from ckan.lib.navl.dictization_functions import Invalid, validate
from ckan.model import Session
from ckan.plugins import PluginImplementations
from ckan.lib.navl.dictization_functions import missing
from ckan.plugins import toolkit as tk

from ckanext.harvest.model import HarvestSource, UPDATE_FREQUENCIES, HarvestJob
from ckanext.harvest.interfaces import IHarvester

_ = tk._
log = logging.getLogger(__name__)


def harvest_source_id_exists(value, context):

    result = HarvestSource.get(value,None)

    if not result:
        raise Invalid('Harvest Source with id %r does not exist.' % str(value))
    return value

def harvest_job_exists(value, context):
    """Check if a harvest job exists and returns the model if it does"""
    result = HarvestJob.get(value, None)

    if not result:
        raise Invalid('Harvest Job with id %r does not exist.' % str(value))
    return result

def _normalize_url(url):
    '''Strips off parameters off a URL, and an unnecessary port number, so that
    simple variations on a URL are ignored, to used to help avoid getting two
    harvesters for the same URL.'''
    o = urlparse.urlparse(url)

    # Normalize port
    if ':' in o.netloc:
        parts = o.netloc.split(':')
        if (o.scheme == 'http' and parts[1] == '80') or \
           (o.scheme == 'https' and parts[1] == '443'):
            netloc = parts[0]
        else:
            netloc = ':'.join(parts)
    else:
        netloc = o.netloc
    
    # Remove trailing slash
    path = o.path.rstrip('/')

    check_url = urlparse.urlunparse((
            o.scheme,
            netloc,
            path,
            None,None,None))

    return check_url

def harvest_source_url_validator(key,data,errors,context):
    new_url = _normalize_url(data[key])
    source_id = data.get(('id',),'')
    if source_id:
        # When editing a source we need to avoid its own URL
        existing_sources = Session.query(HarvestSource.url,HarvestSource.active) \
                       .filter(HarvestSource.id!=source_id).all()
    else:
        existing_sources = Session.query(HarvestSource.url,HarvestSource.active).all()

    for url,active in existing_sources:
        url = _normalize_url(url)
        if url == new_url:
            raise Invalid('There already is a Harvest Source for this URL: %s' % url)

    return data[key] 

def harvest_source_type_exists(value,context):
    #TODO: use new description interface

    # Get all the registered harvester types
    available_types = []
    for harvester in PluginImplementations(IHarvester):
        info = harvester.info()
        if not info or 'name' not in info:
            log.error('Harvester %r does not provide the harvester name in the info response' % str(harvester))
            continue
        available_types.append(info['name'])


    if not value in available_types:
        raise Invalid('Unknown harvester type: %s. Have you registered a harvester for this type?' % value)
    
    return value

def harvest_source_config_validator(key,data,errors,context):
    harvester_type = data.get(('type',),'')
    for harvester in PluginImplementations(IHarvester):
        info = harvester.info()
        if info['name'] == harvester_type:
            if hasattr(harvester, 'validate_config'):
                try:
                    return harvester.validate_config(data[key])
                except Exception, e:
                    raise Invalid('Error parsing the configuration options: %s' % str(e))
            else:
                return data[key]

def keep_not_empty_extras(key, data, errors, context):
    extras = data.pop(key, {})
    for extras_key, value in extras.iteritems():
        if value:
            data[key[:-1] + (extras_key,)] = value

def harvest_source_extra_validator(key,data,errors,context):
    harvester_type = data.get(('type',),'')  # source_type in okf branch

    #gather all extra fields to use as whitelist of what
    #can be added to top level data_dict
    all_extra_fields = set()
    for harvester in PluginImplementations(IHarvester):
        if not hasattr(harvester, 'extra_schema'):
            continue
        all_extra_fields.update(harvester.extra_schema().keys())

    extra_schema = {'__extras': [keep_not_empty_extras]}
    for harvester in PluginImplementations(IHarvester):
        if not hasattr(harvester, 'extra_schema'):
            continue
        info = harvester.info()
        if not info['name'] == harvester_type:
            continue
        extra_schema.update(harvester.extra_schema())
        break

    extra_data, extra_errors = validate(data.get(key, {}), extra_schema)
    for key in extra_data.keys():
        #only allow keys that appear in at least one harvester
        if key not in all_extra_fields:
            extra_data.pop(key)

    for key, value in extra_data.iteritems():
        data[(key,)] = value

    for key, value in extra_errors.iteritems():
        errors[(key,)] = value

    ## need to get config out of extras as __extra runs
    ## after rest of validation
    package_extras = data.get(('extras',), [])

    for num, extra in enumerate(list(package_extras)):
        if extra['key'] == 'config':
            # remove config extra so we can add back cleanly later
            package_extras.pop(num)
            try:
                config_dict = json.loads(extra.get('value') or '{}')
            except ValueError:
                log.error('Wrong JSON provided in config, skipping')
                config_dict = {}
            break
    else:
        config_dict = {}
    config_dict.update(extra_data)
    if config_dict and not extra_errors:
        config = json.dumps(config_dict)
        package_extras.append(dict(key='config',
                                   value=config))
        data[('config',)] = config
    if package_extras:
        data[('extras',)] = package_extras

def harvest_source_convert_from_config(key,data,errors,context):
    config = data[key]
    if config:
        config_dict = json.loads(config)
        for key, value in config_dict.iteritems():
            data[(key,)] = value

def harvest_source_active_validator(value,context):
    if isinstance(value,basestring):
        if value.lower() == 'true':
            return True
        else:
            return False
    return bool(value)

def harvest_source_frequency_exists(value):
    if value == '':
        value = 'MANUAL'
    if value.upper() not in UPDATE_FREQUENCIES:
        raise Invalid('Frequency %s not recognised' % value)
    return value.upper()


def harvest_object_extras_validator(value, context):
    if not isinstance(value, dict):
        raise Invalid('extras must be a dict')
    for v in value.values():
        if not isinstance(v, basestring):
            raise Invalid('extras must be a dict of strings')
    return value

# Based on package_name_validator
def harvest_name_validator(key, data, errors, context):
    model = context["model"]
    session = context["session"]
    harvest_source = context.get("harvest_source")

    query = session.query(HarvestSource.name).filter_by(name=data[key])
    if harvest_source:
        source_id = harvest_source.id
    else:
        source_id = data.get(key[:-1] + ("id",))
    if source_id and source_id is not missing:
        query = query.filter(HarvestSource.id != source_id)
    result = query.first()
    if result:
        errors[key].append(_('That name is already in use.'))

    value = data[key]
    if len(value) < model.PACKAGE_NAME_MIN_LENGTH:
        raise Invalid(
            _('Name "%s" length is less than minimum %s') % (value, model.PACKAGE_NAME_MIN_LENGTH)
        )
    if len(value) > model.PACKAGE_NAME_MAX_LENGTH:
        raise Invalid(
            _('Name "%s" length is more than maximum %s') % (value, model.PACKAGE_NAME_MAX_LENGTH)
        )
