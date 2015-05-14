import logging
import datetime
import socket

from carrot.connection import BrokerConnection
from carrot.messaging import Publisher
from carrot.messaging import Consumer

from ckan.lib.base import config
from ckan.plugins import PluginImplementations

from ckanext.harvest.model import (HarvestJob, HarvestObject,
                                   HarvestGatherError,
                                   HarvestObjectError)
from ckanext.harvest.interfaces import IHarvester
from ckan import model

log = logging.getLogger(__name__)
assert not log.disabled

__all__ = ['get_gather_publisher', 'get_gather_consumer',
           'get_fetch_publisher', 'get_fetch_consumer']

PORT = 5672
USERID = 'guest'
PASSWORD = 'guest'
HOSTNAME = 'localhost'
VIRTUAL_HOST = '/'

# settings for AMQP
EXCHANGE_TYPE = 'direct'
EXCHANGE_NAME = 'ckan.harvest'

def get_carrot_connection():
    backend = config.get('ckan.harvest.mq.library', 'pyamqplib')
    log.debug("Carrot connection using %s backend" % backend)
    try:
        port = int(config.get('ckan.harvest.mq.port', PORT))
    except ValueError:
        port = PORT
    userid = config.get('ckan.harvest.mq.user_id', USERID)
    password = config.get('ckan.harvest.mq.password', PASSWORD)
    hostname = config.get('ckan.harvest.mq.hostname', HOSTNAME)
    virtual_host = config.get('ckan.harvest.mq.virtual_host', VIRTUAL_HOST)

    backend_cls = 'carrot.backends.%s.Backend' % backend
    return BrokerConnection(hostname=hostname, port=port,
                            userid=userid, password=password,
                            virtual_host=virtual_host,
                            backend_cls=backend_cls)

def resubmit_jobs():
    if config.get('ckan.harvest.mq.type') != 'redis':
        return

def get_publisher(routing_key):
    return Publisher(connection=get_carrot_connection(),
                     exchange=EXCHANGE_NAME,
                     exchange_type=EXCHANGE_TYPE,
                     routing_key=routing_key)

def get_consumer(queue_name, routing_key):
    connection = get_carrot_connection()
    try:
        return Consumer(connection=connection,
                        queue=queue_name,
                        routing_key=routing_key,
                        exchange=EXCHANGE_NAME,
                        exchange_type=EXCHANGE_TYPE,
                        durable=True, auto_delete=False)
    except socket.error, e:
        log.error('Error connecting to RabbitMQ with settings: %r',
                  connection.__dict__)
        raise

def gather_callback(message_data, message):
    try:
        id = message_data['harvest_job_id']
    except KeyError:
        log.error('No harvest job id received')
        return
    log.debug('Received harvest job id: %s' % id)

    # Get rid of any old session state that may still be around. This is
    # a simple alternative to creating a new session for this callback.
    model.Session.expire_all()

    # Get a publisher for the fetch queue
    publisher = get_fetch_publisher()

    try:
        job = HarvestJob.get(id)
    except Exception, e:
        # I have occasionally seen:
        # sqlalchemy.exc.OperationalError "SSL connection has been closed unexpectedly"
        log.exception(e)
        log.error('Connection Error during gather of %s: %r %r' % (id, e, e.args))
        # By not sending the message.ack(), it will be retried by RabbitMQ
        # later.
        # Try to clear the issue with a remove
        model.Session.remove()
        return

    try:
        try:
            if not job:
                log.error('Harvest job does not exist: %s' % id)
                return

            # Send the harvest job to the plugins that implement
            # the Harvester interface, only if the source type
            # matches
            harvester_found = False
            for harvester in PluginImplementations(IHarvester):
                if harvester.info()['name'] == job.source.type:
                    harvester_found = True
                    # Get a list of harvest object ids from the plugin
                    job.gather_started = datetime.datetime.now()
                    job.save()
                    try:
                        harvest_object_ids = harvester.gather_stage(job)
                    except (Exception, KeyboardInterrupt), e:
                        # Assume it is a serious error and the harvest stops
                        # now, so tidy up.
                        log.exception(e)
                        log.error('Gather exception: %r', e)
                        job.status = 'Aborted'
                        # Delete any harvest objects else they'd suggest the
                        # job is in limbo
                        harvest_objects = model.Session.query(HarvestObject). \
                            filter_by(harvest_job_id=job.id)
                        for harvest_object in harvest_objects.all():
                            model.Session.delete(harvest_object)
                        model.Session.commit()
                        raise
                    finally:
                        job.gather_finished = datetime.datetime.now()
                        job.save()
                    log.debug('Received objects from plugin''s gather_stage (%d): %r',
                              len(harvest_object_ids or []), harvest_object_ids)

                    # Delete any stray harvest_objects not returned by
                    # gather_stage() - they'd not be dealt with so would
                    # suggest the job is in limbo
                    saved_harvest_object_ids = \
                        model.Session.query(HarvestObject.id)\
                        .filter_by(harvest_job_id=job.id).all()
                    orphaned_harvest_objects_ids = \
                        set(saved_harvest_object_ids) - \
                        set(harvest_object_ids or [])
                    if orphaned_harvest_objects_ids:
                        log.warning('Orphaned objects deleted (%d): %s',
                                    len(orphaned_harvest_objects_ids),
                                    orphaned_harvest_objects_ids)
                        for obj_id in orphaned_harvest_objects_ids:
                            model.Session.delete(HarvestObject.get(obj_id))
                        model.Session.commit()

                    if harvest_object_ids and len(harvest_object_ids) > 0:
                        for id in harvest_object_ids:
                            # Send the id to the fetch queue
                            publisher.send({'harvest_object_id':id})
                            log.debug('Sent object %s to the fetch queue' % id)

            if not harvester_found:
                # This can occur if you:
                # * remove a harvester and it still has sources that are then
                #   refreshed
                # * add a new harvester and restart CKAN but not the gather
                #   queue.
                msg = 'System error - no harvester could be found for source'\
                      ' type %s' % job.source.type
                HarvestGatherError.create(message=msg, job=job)
                log.error(msg)
                job.status = 'Aborted'
                job.save()

        finally:
            publisher.close()

    finally:
        message.ack()


def fetch_callback(message_data, message):
    try:
        id = message_data['harvest_object_id']
    except KeyError:
        log.error('No harvest object id received')
        message.ack()
        return
    log.info('Received harvest object id: %s' % id)

    # Get rid of any old session state that may still be around. This is
    # a simple alternative to creating a new session for this callback.
    model.Session.expire_all()

    try:
        obj = HarvestObject.get(id)
    except Exception, e:
        # I quite often see:
        # sqlalchemy.exc.OperationalError "server closed the connection unexpectedly"
        # followed by sqlalchemy.exc.StatementError "Can't reconnect until invalid transaction is rolled back"
        log.exception(e)
        log.error('Connection Error during fetch of %s: %r %r' % (id, e, e.args))
        # By not sending the message.ack(), it will be retried by RabbitMQ
        # later.
        # Try to clear the issue with a remove
        model.Session.remove()
        return

    try:
        if not obj:
            log.error('Harvest object does not exist: %s' % id)
            return
        # Send the harvest object to the plugins that implement
        # the Harvester interface, only if the source type
        # matches
        for harvester in PluginImplementations(IHarvester):
            if harvester.info()['name'] == obj.source.type:

                # See if the plugin can fetch the harvest object
                obj.fetch_started = datetime.datetime.now()
                obj.fetch_started = datetime.datetime.utcnow()
                obj.state = "FETCH"
                obj.save()
                try:
                    success_fetch = harvester.fetch_stage(obj)
                except Exception, e:
                    msg = 'System error (%s)' % e
                    HarvestObjectError.create(
                        message=msg, object=obj, stage='Fetch', line=None)
                    log.error('Fetch exception %s obj=%s guid=%s source=%s',
                              e, obj.id, obj.guid, obj.source.id)
                    log.exception(e)
                    success_fetch = False
                finally:
                    obj.fetch_finished = datetime.datetime.now()
                    obj.save()
                #TODO: retry times?
                if success_fetch == True:
                    # If no errors were found, call the import method
                    obj.import_started = datetime.datetime.utcnow()
                    obj.state = "IMPORT"
                    obj.save()
                    try:
                        success_import = harvester.import_stage(obj)
                    except Exception, e:
                        msg = 'System error (%s)' % e
                        HarvestObjectError.create(
                            message=msg, object=obj, stage='Import', line=None)
                        log.error('Import exception: %s obj=%s guid=%s source=%s',
                                  e, obj.id, obj.guid, obj.source.id)
                        log.exception(e)
                        success_import = False
                    finally:
                        obj.import_finished = datetime.datetime.utcnow()
                        obj.save()
                    if success_import:
                        obj.state = "COMPLETE"
                    else:
                        obj.state = "ERROR"
                    obj.save()
                elif success_fetch == 'unchanged':
                    obj.report_status = 'unchanged'
                    obj.state = "COMPLETE"
                    obj.save()
                else:
                    obj.state = "ERROR"
                    obj.save()
                if obj.report_status:
                    return
                if obj.state == 'ERROR':
                    obj.report_status = 'errored'
                elif obj.get_extra('status') == 'deleted':
                    obj.report_status = 'deleted'
                elif obj.current == False:
                    # decided not to continue with the import after all
                    obj.report_status = 'unchanged'
                elif len(model.Session.query(HarvestObject)
                    .filter_by(package_id = obj.package_id)
                    .limit(2)
                    .all()) == 2:
                    obj.report_status = 'reimported'
                else:
                    obj.report_status = 'new'
                obj.save()
    finally:
        message.ack()

def get_gather_consumer():
    consumer = get_consumer('ckan.harvest.gather','harvest_job_id')
    consumer.register_callback(gather_callback)
    log.debug('Gather queue consumer registered')
    return consumer

def get_fetch_consumer():
    consumer = get_consumer('ckan.harvert.fetch','harvest_object_id')
    consumer.register_callback(fetch_callback)
    log.debug('Fetch queue consumer registered')
    return consumer

def get_gather_publisher():
    return get_publisher('harvest_job_id')

def get_fetch_publisher():
    return get_publisher('harvest_object_id')

# Get a publisher for the fetch queue
#fetch_publisher = get_fetch_publisher()

