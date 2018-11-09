#!/usr/bin/env python 
import os, sys, time, json, requests, logging
import hashlib
from datetime import datetime
from hysds_commons.job_utils import resolve_hysds_job
from hysds.celery import app
import util
import uuid  # only need this import to simulate returned mozart job id
from hysds.celery import app
from hysds_commons.job_utils import submit_mozart_job
import traceback


# set logger
log_format = "[%(asctime)s: %(levelname)s/%(name)s/%(funcName)s] %(message)s"
logging.basicConfig(format=log_format, level=logging.INFO)

class LogFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'id'): record.id = '--'
        return True

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])
logger.setLevel(logging.INFO)
logger.addFilter(LogFilter())


IFG_CFG_ID_TMPL = "ifg-cfg_R{}_M{:d}S{:d}_TN{:03d}_{:%Y%m%dT%H%M%S}-{:%Y%m%dT%H%M%S}-{}-{}"

BASE_PATH = os.path.dirname(__file__)
MOZART_URL = app.conf['MOZART_URL']
MOZART_ES_ENDPOINT = "MOZART"
GRQ_ES_ENDPOINT = "GRQ"
sleep_seconds = 120
slc_check_max_sec = 300
sling_completion_max_sec = 11000


class ACQ:
    def __init__(self, acq_id, acq_type):
	self.acq_id=acq_id
	self.acq_type = acq_type

def get_acq_object(acq_id, acq_type):
    return {
        "acq_id": acq_id,
        "acq_type":  acq_type
    }

def get_area(coords):
    '''get area of enclosed coordinates- determines clockwise or counterclockwise order'''
    n = len(coords) # of corners
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += coords[i][1] * coords[j][0]
        area -= coords[j][1] * coords[i][0]
    #area = abs(area) / 2.0
    return area / 2


def query_es(endpoint, doc_id):
    """
    This function queries ES
    :param endpoint: the value specifies which ES endpoint to send query
     can be MOZART or GRQ
    :param doc_id: id of product or job
    :return: result from elasticsearch
    """
    es_url, es_index = None, None
    if endpoint == GRQ_ES_ENDPOINT:
        es_url = app.conf["GRQ_ES_URL"]
        es_index = "grq"
    if endpoint == MOZART_ES_ENDPOINT:
        es_url = app.conf['JOBS_ES_URL']
        es_index = "job_status-current"

    query = {
        "query": {
            "bool": {
                "must": [
                    {"term": {"_id": doc_id}} # add job status:
                ]
            }
        }
    }

    #ES = elasticsearch.Elasticsearch(es_url)
    #result = ES.search(index=es_index, body=query)

    if es_url.endswith('/'):
        search_url = '%s%s/_search' % (es_url, es_index)
    else:
        search_url = '%s/%s/_search' % (es_url, es_index)
    r = requests.post(search_url, data=json.dumps(query))

    if r.status_code != 200:
        print("Failed to query %s:\n%s" % (es_url, r.text))
        print("query: %s" % json.dumps(query, indent=2))
        print("returned: %s" % r.text)
        r.raise_for_status()

    result = r.json()

    if len(result["hits"]["hits"]) == 0:
        raise ValueError("Couldn't find record with ID: %s, at ES: %s"%(doc_id, es_url))
        return

    #LOGGER.debug("Got: {0}".format(json.dumps(result)))
    return result


def create_dataset_json(id, version, met_file, ds_file):
    """Write dataset json."""

    logger.info("create_dataset_json")

    # get metadata
    with open(met_file) as f:
        md = json.load(f)

    ds = {
        'creation_timestamp': "%sZ" % datetime.utcnow().isoformat(),
        'version': version,
        'label': id
    }

    coordinateste_dataset_json = None

    try:

        coordinates = md['union_geojson']['coordinates']
    
        cord_area = get_area(coordinates[0])
        if not cord_area>0:
            logger.info("creating dataset json. coordinates are not clockwise, reversing it")
            coordinates = [coordinates[0][::-1]]
            logger.info(coordinates)
            cord_area = get_area(coordinates[0])
            if not cord_area>0:
                logger.info("creating dataset json. coordinates are STILL NOT  clockwise")
        else:
            logger.info("creating dataset json. coordinates are already clockwise")

        ds['location'] =  {'type': 'Polygon', 'coordinates': coordinates}
        logger.info("create_dataset_json location : %s" %ds['location'])

    except Exception as err:
        logger.info("create_dataset_json: Exception : ")
        logger.warn(str(err))
        logger.warn("Traceback: {}".format(traceback.format_exc()))


    ds['starttime'] = md['starttime']
    ds['endtime'] = md['endtime']

    # write out dataset json
    with open(ds_file, 'w') as f:
        json.dump(ds, f, indent=2)

def get_job_status(job_id):
    """
    This function gets the staged products and context of previous PGE job
    :param job_id: this is the id of the job on mozart
    :return: tuple(products_staged, prev_context, message)
    the message refects the
    """
    endpoint = MOZART_ES_ENDPOINT
    return_job_id = None
    return_job_status = None

    #check if Jobs ES has updated job status
    if check_ES_status(job_id):
        response = query_es(endpoint, job_id)

    result = response["hits"]["hits"][0]
    message = None  #using this to store information regarding deduped jobs, used later to as error message unless it's value is "success"

    #print ("Job INFO retrieved from ES: %s"%json.dumps(result))
    #print ("Type of status from ES: %s"%type(result["_source"]["status"]))
    status = str(result["_source"]["status"])
    if status == "job-deduped":
        #query ES for the original job's status
        orig_job_id = result["_source"]["dedup_job"]
        return_job_id = orig_job_id
        orig_job_info = query_es(endpoint, orig_job_id)
        """check if original job failed -> this would happen when at the moment of deduplication, the original job
         was in 'running state', but soon afterwards failed. So, by the time the status is checked in this function,
         it may be shown as failed."""
        #print ("Original JOB info: \n%s"%json.dumps(orig_job_info))
        orig_job_info = orig_job_info["hits"]["hits"][0]
        orig_job_status = str(orig_job_info["_source"]["status"])
	logger.info("Check Job Status : Job %s was Deduped. The new/origianl job id is %s whose status is : %s" %(job_id, return_job_id, return_job_status)) 
	return_job_status = orig_job_status

        if  orig_job_status == "job-failed":
            message = "Job was deduped against a failed job with id: %s, please retry job."%orig_job_id
            logger.info(message) 
        elif orig_job_status == "job-started" or orig_job_status == "job-queued":
            logger.info ("Job was deduped against a queued/started job with id: %s. Please look at already running job with same params."%orig_job_id)
            message = "Job was deduped against a queued/started job with id: %s. Please look at already running job with same params."%orig_job_id
        elif orig_job_status == "job-completed":
            # return products staged and context of original job
            message = "success"
    else:
	return_job_id = job_id
    	return_job_status = result["_source"]["status"]

    return return_job_status, return_job_id

def check_ES_status(doc_id):
    """
    There is a latency in the update of ES job status after
    celery signals job completion.
    To handle that case, we much poll ES (after sciflo returns status after blocking)
    until the job status is correctly reflected.
    :param doc_id: ID of the Job ES doc
    :return: True  if the ES has updated job status within 5 minutes
            otherwise raise a run time error
    """
    es_url = app.conf['JOBS_ES_URL']
    es_index = "job_status-current"
    query = {
        "_source": [
                   "status"
               ],
        "query": {
            "bool": {
                "must": [
                    {"term": {"_id": doc_id}}
                ]
            }
        }
    }

    #ES = elasticsearch.Elasticsearch(es_url)
    #result = ES.search(index=es_index, body=query)
    if es_url.endswith('/'):
        search_url = '%s%s/_search' % (es_url, es_index)
    else:
        search_url = '%s/%s/_search' % (es_url, es_index)
    r = requests.post(search_url, data=json.dumps(query))

    if r.status_code != 200:
        print("Failed to query %s:\n%s" % (es_url, r.text))
        print("query: %s" % json.dumps(query, indent=2))
        print("returned: %s" % r.text)
        r.raise_for_status()

    result = r.json()


    sleep_seconds = 2
    timeout_seconds = 300
    # poll ES until job status changes from "job-started" or for the job doc to show up. The poll will timeout soon after 5 mins.

    while len(result["hits"]["hits"]) == 0: #or str(result["hits"]["hits"][0]["_source"]["status"]) == "job-started":
        if sleep_seconds >= timeout_seconds:
            if len(result["hits"]["hits"]) == 0:
                raise RuntimeError("ES taking too long to index job with id %s."%doc_id)
            else:
                raise RuntimeError("ES taking too long to update status of job with id %s."%doc_id)
        time.sleep(sleep_seconds)
        #result = ES.search(index=es_index, body=query)

        r = requests.post(search_url, data=json.dumps(query))

        if r.status_code != 200:
            print("Failed to query %s:\n%s" % (es_url, r.text))
            print("query: %s" % json.dumps(query, indent=2))
            print("returned: %s" % r.text)
            r.raise_for_status()

        result = r.json()
        sleep_seconds = sleep_seconds * 2

    logging.info("Job status updated on ES to %s"%str(result["hits"]["hits"][0]["_source"]["status"]))
    return True

def check_slc_status(slc_id, index_suffix):

    result = util.get_dataset(slc_id, index_suffix)
    total = result['hits']['total']

    if total > 0:
	return True

    return False

def check_slc_status(slc_id):

    result = util.get_dataset(slc_id)
    total = result['hits']['total']

    if total > 0:
        return True

    return False


def resolve_source(ctx_file):
    """Resolve best URL from acquisition."""


    # get settings
    # read in context
    with open(ctx_file) as f:
        ctx = json.load(f)
    
    '''
    settings_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'settings.json')
    with open(settings_file) as f:
        settings = json.load(f)
    '''

    sleep_seconds = 30
    

    # build args
    project = ctx["input_metadata"]["project"]
    if type(project) is list:
        project = project[0]
    dem_type= ctx["input_metadata"]["dem_type"]
    track = ctx["input_metadata"]["track"]
    master_acqs = [i.strip() for i in ctx["input_metadata"]['master_acquisitions'].split()]
    slave_acqs = [i.strip() for i in ctx["input_metadata"]['slave_acquisitions'].split()]
    logger.info("master_acqs : %s" %master_acqs)
    logger.info("slave_acqs : %s" %slave_acqs)


    master_scene = ctx["input_metadata"]["master_scenes"]   
    slave_scene = ctx["input_metadata"]["slave_scenes"]
    starttime = ctx["input_metadata"]["starttime"]
    endtime = ctx["input_metadata"]["endtime"]
    bbox = None
    if "bbox" in ctx["input_metadata"]:
        bbox = ctx["input_metadata"]["bbox"]

    union_geojson = ctx["input_metadata"]["union_geojson"]
 
    spyddder_extract_version = ctx["spyddder_extract_version"]
    acquisition_localizer_version = ctx["acquisition_localizer_version"]
    ''' 
    spyddder_extract_version = ctx["input_metadata"]["spyddder_extract_version"]
    acquisition_localizer_version = ctx["input_metadata"]["acquisition_localizer_version"]
    standard_product_ifg_version = ctx["input_metadata"]["standard_product_ifg_version"]
    '''
    job_priority = ctx["input_metadata"]["job_priority"]
    job_type, job_version = ctx['job_specification']['id'].split(':') 

    queues = []  # where should we get the queue value
    identifiers = []
    prod_dates = []
   

    acq_info = {}
    
    index_suffix = "S1-IW_ACQ"



    # Find out status of all Master ACQs, create a ACQ object with that and update acq_info dictionary
    for acq in master_acqs:
 	acq_type = "master"
        acq_info[acq]=get_acq_object(acq, acq_type)

    # Find out status of all Slave ACQs, create a ACQ object with that and update acq_info dictionary
    for acq in slave_acqs:
	acq_type = "slave"
	acq_info[acq]=get_acq_object(acq, acq_type)

    '''
    acq_infoes =[]
    projects = []
    job_priorities = []
    job_types = []
    job_versions = []
    spyddder_extract_versions = []
    acquisition_localizer_versions = []
    #standard_product_ifg_versions = []
    starttimes = []
    endtimes = []
    bboxes = []
    union_geojsons =[]
    master_scenes = []
    slave_scenes = []
    id_hashes = []
    id_hashes.append(id_hash)
    master_scenes.append(master_scene)
    slave_scenes.append(slave_scene)


    acq_infoes.append(acq_info)
    projects.append(project)
    job_priorities.append(job_priority)
    job_types.append(job_type)
    job_versions.append(job_version)
    spyddder_extract_versions.append(spyddder_extract_version)
    acquisition_localizer_versions.append(acquisition_localizer_version)
    #standard_product_ifg_versions.append(standard_product_ifg_version)
    starttimes.append(starttime)
    endtimes.append(endtime)
    union_geojsons.append(union_geojson)
    if bbox:
        bboxes.append(bbox)
    '''
    #return acq_infoes, spyddder_extract_versions, acquisition_localizer_versions, standard_product_ifg_versions, projects, job_priorities, job_types, job_versions
    return acq_info, spyddder_extract_version, acquisition_localizer_version, project, job_priority, job_type, job_version, dem_type, track, starttime, endtime, master_scene, slave_scene, union_geojson, bbox


def sling(acq_info, spyddder_extract_version, acquisition_localizer_version, project, job_priority, job_type, job_version, dem_type, track, starttime, endtime, master_scene, slave_scene, union_geojson, bbox):
    '''
	This function checks if any ACQ that has not been ingested yet and sling them.
    '''
    #logger.info("acq_info type: %s : %s" %(type(acq_info), len(acq_info) ))
    #logger.info(acq_info)
    logger.info("%s : %s" %(type(spyddder_extract_version), spyddder_extract_version))

    id_hash = get_id_hash(acq_info, job_priority, dem_type)
    acq_list = acq_info.keys()
    job_id = submit_sling_job(id_hash, project, spyddder_extract_version, acquisition_localizer_version, acq_list, job_priority)
    logger.info("\nSUBMITTED Acquisition Localizer Job with id : %s" %job_id)
    # Now loop in until all the jobs are completed 
    job_done = False
    job_check_start_time = datetime.utcnow()

    while not all_done:

        job_status, job_id  = get_job_status(job_id)  
        logger.info("\nAcquisition Localizer Job id Now : %s with status : %s" %(job_id, job_status))
        if job_status == "job-completed":
            logger.info("Success! sling job with job id : %s COMPLETED!!" %job_id)
            job_done = True
        elif job_status == "job-failed":
            err_msg = "Error : Acquisition Localizer job %s FAILED. So existing out of the sciflo!!....." %job_id
	    logger.info(err_msg)
            raise RuntimeError(err_msg)
        else:
            logger.info("Sling Job RUNNING id : %s. Job Status : %s" %(job_id, job_status))

	    now = datetime.utcnow()
	    delta = (now - job_check_start_time).total_seconds()
            if delta >= sling_completion_max_sec:
            	raise RuntimeError("Error : Sling jobs NOT completed after %.2f hours!!" %(delta/3600))
	    logger.info("All job not completed. So sleeping for %s seconds" %sleep_seconds)
	    time.sleep(sleep_seconds)


    # At this point, we have all the slc downloaded and we are ready to submit a create standard product job
    acq_infoes =[]
    projects = []
    job_priorities = []
    job_types = []
    job_versions = []
    #standard_product_ifg_versions = []
    dem_types = []
    tracks = []
    starttimes = []
    endtimes = []
    bboxes = []
    union_geojsons =[]
    master_scenes = []
    slave_scenes = []
    master_scenes.append(master_scene)
    slave_scenes.append(slave_scene)

    acq_infoes.append(acq_info)
    projects.append(project)
    job_priorities.append(job_priority)
    #standard_product_ifg_versions.append(standard_product_ifg_version)
    dem_types.append(dem_type)
    tracks.append(track)
    starttimes.append(starttime)
    endtimes.append(endtime)
    union_geojsons.append(union_geojson)
    if bbox:
        bboxes.append(bbox)

    return acq_infoes, projects, job_priorities, dem_types, tracks, starttimes, endtimes, master_scenes, slave_scenes, union_geojsons, bboxes

def get_id_hash(acq_info, job_priority, dem_type):
    id_hash = ""
    master_ids_str=""
    slave_ids_str=""


    for acq in acq_info.keys():
        acq_type = acq_info[acq]['acq_type']

	if acq_type == "master":
	    if master_ids_str=="":
		master_ids_str=acq
	    else:
		master_ids_str += " "+acq

	elif acq_type == "slave":
            if slave_ids_str=="":
                slave_ids_str=acq
            else:
                slave_ids_str += " "+acq


    logger.info("master_ids_str : %s" %master_ids_str)
    logger.info("slave_ids_str : %s" %slave_ids_str)

       
    id_hash = hashlib.md5(json.dumps([
	job_priority,
	master_ids_str,
	slave_ids_str,
        dem_type
    ]).encode("utf8")).hexdigest()

    return id_hash

        

def check_all_job_completed(acq_info):
    all_done = True
    for acq_id in acq_info.keys():
        if not acq_info[acq_id]['localized']:  
	    job_status = acq_info[acq_id]['job_status']
	    if not job_status == "job-completed":
		logger.info("check_all_job_completed : %s NOT completed!!" %acq_info[acq_id]['job_id'])	
		all_done = False
		break
    return all_done




def publish_localized_info( acq_info, project, job_priority, dem_type, track, starttime, endtime, master_scene, slave_scene, union_geojson, bbox, wuid=None, job_num=None):
    for i in range(len(project)):
        publish_data( acq_info[i], project[i], job_priority[i], dem_type[i], track[i], starttime[i], endtime[i], master_scene[i], slave_scene[i], union_geojson[i], bbox[i])

def publish_data( acq_info, project, job_priority, dem_type, track,starttime, endtime, master_scene, slave_scene, union_geojson, bbox, wuid=None, job_num=None):
    """Map function for create interferogram job json creation."""

    logger.info("\n\n\n PUBLISH IFG JOB!!!")
    logger.info("project : %s " %project)
    logger.info("dem type : %s " %dem_type)
    logger.info("track : %s" %track)
    logger.info("starttime, endtime, : %s : %s " %(starttime, endtime))
    logger.info(" master_scene, slave_scene : %s, %s" %(master_scene, slave_scene))
    logger.info(" union_geojson : %s, bbox : %s " %( union_geojson, bbox))
   
    #version = get_version()
    version = "v2.0.0"

    if type(project) is list:
        project = project[0]
    logger.info("project : %s" %project)

    '''
    master_ids_str=""
    master_ids_list=[]

    slave_ids_str=""
    slave_ids_list=[]

    master_acq_list = []
    slave_acq_list = []

    for acq in acq_info.keys():
	acq_data = acq_info[acq]['acq_data']
	acq_type = acq_info[acq]['acq_type']
	identifier =  acq_data["metadata"]["identifier"]
        logger.info("identifier : %s" %identifier)
	if acq_type == "master":
	    master_ids_list.append(identifier)
            master_acq_list.append(acq)
 
	    if master_ids_str=="":
		master_ids_str=identifier
	    else:
		master_ids_str += " "+identifier

	elif acq_type == "slave":
            slave_acq_list.append(acq)
            slave_ids_list.append(identifier)
            if slave_ids_str=="":
                slave_ids_str=identifier
            else:
                slave_ids_str += " "+identifier


    logger.info("master_ids_str : %s" %master_ids_str)
    logger.info("slave_ids_str : %s" %slave_ids_str)
    '''


    # set job type and disk space reqs
    disk_usage = "300GB"

    # set job queue based on project
    job_queue = "%s-job_worker-large" % project
    list_master_dt = ""
    list_slave_dt = ""
    #job_type = "job-standard-product-ifg:%s" %standard_product_ifg_version
    try:
        list_master_dt, list_slave_dt = util.get_acq_dates(master_scene, slave_scene)
    except Exception as err:
         logger.info(str(err))
       

    id_hash = get_id_hash(acq_info, job_priority, dem_type)

    orbit_type = 'poeorb'

    id = IFG_CFG_ID_TMPL.format('M', len(master_scene), len(slave_scene), track, list_master_dt, list_slave_dt, orbit_type, id_hash[0:4])

    #id = "standard-product-ifg-cfg-%s" %id_hash[0:4]
    prod_dir =  id
    os.makedirs(prod_dir, 0o755)

    met_file = os.path.join(prod_dir, "{}.met.json".format(id))
    ds_file = os.path.join(prod_dir, "{}.dataset.json".format(id))
  
    #with open(met_file) as f: md = json.load(f)
    md = {}
    md['id'] = id
    md['project'] =  project,
    md['master_ids'] = master_ids_str
    md['slave_ids'] = slave_ids_str
    #md['standard_product_ifg_version'] = standard_product_ifg_version
    md['priority'] = job_priority
    md['azimuth_looks'] = 19
    md['range_looks'] = 7
    md['filter_strength'] =  0.5
    md['precise_orbit_only'] = 'true'
    md['auto_bbox'] = 'true'
    md['_disk_usage'] = disk_usage
    md['soft_time_limit'] =  86400
    md['time_limit'] = 86700
    md['dem_type'] = dem_type
    md['track'] = track
    md['starttime'] = starttime
    md['endtime'] = endtime
    md['union_geojson'] = union_geojson
    md['master_scenes'] = master_scene
    md['slave_scenes'] = slave_scene

    if bbox:
        md['bbox'] = bbox

    with open(met_file, 'w') as f: json.dump(md, f, indent=2)


    print("creating dataset file : %s" %ds_file)
    create_dataset_json(id, version, met_file, ds_file)

def submit_ifg_job( acq_info, project, standard_product_ifg_version, job_priority, wuid=None, job_num=None):
    """Map function for create interferogram job json creation."""

    if wuid is None or job_num is None:
        raise RuntimeError("Need to specify workunit id and job num.")
    logger.info("\n\n\n SUBMIT IFG JOB!!!")
    
    logger.info("project : %s" %project)

    master_ids_str=""
    master_ids_list=[]

    slave_ids_str=""
    slave_ids_list=[]


    logger.info("project : %s" %project)

    for acq in acq_info.keys():
	acq_data = acq_info[acq]['acq_data']
	acq_type = acq_info[acq]['acq_type']
	identifier =  acq_data["metadata"]["identifier"]
        logger.info("identifier : %s" %identifier)
	if acq_type == "master":
	    master_ids_list.append(identifier)
	    if master_ids_str=="":
		master_ids_str=identifier
	    else:
		master_ids_str += " "+identifier

	elif acq_type == "slave":
            slave_ids_list.append(identifier)
            if slave_ids_str=="":
                slave_ids_str=identifier
            else:
                slave_ids_str += " "+identifier


    logger.info("master_ids_str : %s" %master_ids_str)
    logger.info("slave_ids_str : %s" %slave_ids_str)
    # set job type and disk space reqs
    disk_usage = "300GB"

    # set job queue based on project
    job_queue = "%s-job_worker-large" % project
   
    job_type = "job-standard-product-ifg"

    job_hash = hashlib.md5(json.dumps([
	job_priority,
	master_ids_str,
	slave_ids_str
    ])).hexdigest()




    return {
        "job_name": "%s-%s" % (job_type, job_hash[0:4]),
        "job_type": "job:%s" % job_type,
        "job_queue": job_queue,
        "container_mappings": {
            "/home/ops/.netrc": "/home/ops/.netrc",
            "/home/ops/.aws": "/home/ops/.aws",
            "/home/ops/verdi/etc/settings.conf": "/home/ops/ariamh/conf/settings.conf"
        },    
        "soft_time_limit": 86400,
        "time_limit": 86700,
        "payload": {
            # sciflo tracking info
            "_sciflo_wuid": wuid,
            "_sciflo_job_num": job_num,

            # job params
            "project": project,
            "master_ids": master_ids_str,
	    "slave_ids": slave_ids_str,
	    "job_priority" : job_priority,
	    "azimuth_looks" : 19,
	    "range_looks" : 7,
	    "filter_strength" : 0.5,
	    "precise_orbit_only" : "true",
	    "auto_bbox" : "true",
	    "priority" : job_priority,

            # v2 cmd
            "_command": "/home/ops/ariamh/interferogram/sentinel/sciflo_create_standard_product.sh",

            # disk usage
            "_disk_usage": disk_usage,

        }
    }

def submit_sling_job(id_hash, project, spyddder_extract_version, acquisition_localizer_versions, acq_data, priority):

    """Map function for spyddder-man extract job."""

    acquisition_localizer_version = "standard-product"
    spyddder_extract_version = "standard-product"
    acquisition_localizer_multi_source_version = "master"
    job_submit_url = '%s/mozart/api/v0.1/job/submit' % MOZART_URL

    # set job type and disk space reqs
    job_type = "acquisition_localizer_multi_source:{}".format(acquisition_localizer_multi_source_version)
    job_type = "acquisition_localizer_multi_source:master"
     # set job type and disk space reqs
    disk_usage = "300GB"
    #logger.info(acq_data)
    #acq_id = acq_data['acq_id']

    # set job queue based on project
    #job_queue = "%s-job_worker-large" % project
    job_queue = "factotum-job_worker-small" 
    rule = {
        "rule_name": "standard-product-sling",
        "queue": job_queue,
        "priority": '5',
        "kwargs":'{}'
    }

    sling_job_name = "standard_product-%s-%s" %(job_type, id_hash )


    params = [
        {
            "name": "project",
            "from": "value",
            "value": project
        },
        {
            "name": "spyddder_extract_version",
            "from": "value",
            "value": spyddder_extract_version
        },
        {
            "name": "acquisition_localizer_version",
            "from": "value",
            "value": acquisition_localizer_version
        }
    ]
    

    logger.info("PARAMS : %s" %params)
    logger.info("RULE : %s"%rule)
    logger.info(job_type)
    logger.info(sling_job_name)

    mozart_job_id = submit_mozart_job({}, rule,hysdsio={"id": "internal-temporary-wiring", "params": params, "job-specification": job_type}, job_name=sling_job_name)
    logger.info("\nSubmitted sling job with id %s for  %s" %(acq_data["metadata"]["identifier"], mozart_job_id))

    return mozart_job_id

def check_ES_status(doc_id):
    """
    There is a latency in the update of ES job status after
    celery signals job completion.
    To handle that case, we much poll ES (after sciflo returns status after blocking)
    until the job status is correctly reflected.
    :param doc_id: ID of the Job ES doc
    :return: True  if the ES has updated job status within 5 minutes
            otherwise raise a run time error
    """
    es_url = app.conf['JOBS_ES_URL']
    es_index = "job_status-current"
    query = {
        "_source": [
                   "status"
               ],
        "query": {
            "bool": {
                "must": [
                    {"term": {"_id": doc_id}}
                ]
            }
        }
    }

    #ES = elasticsearch.Elasticsearch(es_url)
    #result = ES.search(index=es_index, body=query)
    if es_url.endswith('/'):
        search_url = '%s%s/_search' % (es_url, es_index)
    else:
        search_url = '%s/%s/_search' % (es_url, es_index)
    r = requests.post(search_url, data=json.dumps(query))

    if r.status_code != 200:
        print("Failed to query %s:\n%s" % (es_url, r.text))
        print("query: %s" % json.dumps(query, indent=2))
        print("returned: %s" % r.text)
        r.raise_for_status()

    result = r.json()


    sleep_seconds = 2
    timeout_seconds = 300
    # poll ES until job status changes from "job-started" or for the job doc to show up. The poll will timeout soon after 5 mins.

    while len(result["hits"]["hits"]) == 0: #or str(result["hits"]["hits"][0]["_source"]["status"]) == "job-started":
        if sleep_seconds >= timeout_seconds:
            if len(result["hits"]["hits"]) == 0:
                raise RuntimeError("ES taking too long to index job with id %s."%doc_id)
            else:
                raise RuntimeError("ES taking too long to update status of job with id %s."%doc_id)
        time.sleep(sleep_seconds)
        #result = ES.search(index=es_index, body=query)

        r = requests.post(search_url, data=json.dumps(query))

        if r.status_code != 200:
            print("Failed to query %s:\n%s" % (es_url, r.text))
            print("query: %s" % json.dumps(query, indent=2))
            print("returned: %s" % r.text)
            r.raise_for_status()

        result = r.json()
        sleep_seconds = sleep_seconds * 2

    logging.info("Job status updated on ES to %s"%str(result["hits"]["hits"][0]["_source"]["status"]))
    return True
    

def main():
    #master_acqs = ["acquisition-S1A_IW_ACQ__1SDV_20180702T135953_20180702T140020_022616_027345_3578"]
    #slave_acqs = ["acquisition-S1B_IW_ACQ__1SDV_20180720T015751_20180720T015819_011888_015E1C_3C64"]
    master_acqs = ["acquisition-S1A_IW_ACQ__1SDV_20180807T135955_20180807T140022_023141_02837E_DA79"]
    slave_acqs =["acquisition-S1A_IW_ACQ__1SDV_20180714T140019_20180714T140046_022791_027880_AFD3", "acquisition-S1A_IW_ACQ__1SDV_20180714T135954_20180714T140021_022791_027880_D224", "acquisition-S1A_IW_ACQ__1SDV_20180714T135929_20180714T135956_022791_027880_9FCA"]


    #acq_data= util.get_partial_grq_data("acquisition-S1A_IW_ACQ__1SDV_20180702T135953_20180702T140020_022616_027345_3578")['fields']['partial'][0]
    acq_data= util.get_partial_grq_data("acquisition-S1A_IW_SLC__1SSV_20160630T135949_20160630T140017_011941_01266D_C62F")['fields']['partial'][0]
    print(acq_data) 
    
    #resolve_source(master_acqs, slave_acqs)
    print(acq_data["dataset_type"])
    print(acq_data["dataset"])    
    print(acq_data["metadata"]["identifier"]) 
    print(acq_data["metadata"]["download_url"])
    print(acq_data["metadata"]["archive_filename"])
    #print(acq_data["metadata"][""])
if __name__ == "__main__":
    main()



