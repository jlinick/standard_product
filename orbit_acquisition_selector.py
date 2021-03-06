#!/usr/bin/env python3 
import os, sys, time, json, requests, logging
import re, traceback, argparse, copy, bisect
from xml.etree import ElementTree
from UrlUtils import UrlUtils
import util
import gtUtil
from util import ACQ
import datetime  
import dateutil.parser
from datetime import datetime, timedelta
import groundTrack
from osgeo import ogr
import lightweight_water_mask
import csv

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

SLC_RE = re.compile(r'(?P<mission>S1\w)_IW_SLC__.*?' +
                    r'_(?P<start_year>\d{4})(?P<start_month>\d{2})(?P<start_day>\d{2})' +
                    r'T(?P<start_hour>\d{2})(?P<start_min>\d{2})(?P<start_sec>\d{2})' +
                    r'_(?P<end_year>\d{4})(?P<end_month>\d{2})(?P<end_day>\d{2})' +
                    r'T(?P<end_hour>\d{2})(?P<end_min>\d{2})(?P<end_sec>\d{2})_.*$')

BASE_PATH = os.path.dirname(__file__)
MISSION = 'S1A'


def query_es(query, es_index=None):
    logger.info("query: %s" %query)
    """Query ES."""
    uu = UrlUtils()
    es_url = uu.rest_url
    rest_url = es_url[:-1] if es_url.endswith('/') else es_url
    url = "{}/_search?search_type=scan&scroll=60&size=100".format(rest_url)
    if es_index:
        url = "{}/{}/_search?search_type=scan&scroll=60&size=100".format(rest_url, es_index)
    logger.info("url: {}".format(url))
    r = requests.post(url, data=json.dumps(query))
    r.raise_for_status()
    scan_result = r.json()
    #logger.info("scan_result: {}".format(json.dumps(scan_result, indent=2)))
    count = scan_result['hits']['total']
    if count == 0:
        return []


    if '_scroll_id' not in scan_result:
        logger.info("_scroll_id not found in scan_result. Returning empty array for the query :\n%s" %query)
        return []

    scroll_id = scan_result['_scroll_id']
    hits = []
    while True:
        r = requests.post('%s/_search/scroll?scroll=60m' % rest_url, data=scroll_id)
        res = r.json()
        scroll_id = res['_scroll_id']
        if len(res['hits']['hits']) == 0: break
        hits.extend(res['hits']['hits'])
    return hits




def query_aois(starttime, endtime):
    """Query ES for active AOIs that intersect starttime and endtime."""

    es_index = "grq_*_area_of_interest"
    query = {
        "query": {
            "bool": {
                "should": [
                    {
                        "bool": {
                            "must": [
                                {
                                    "range": {
                                        "starttime": {
                                            "lte": endtime
                                        }
                                    }
                                },
                                {
                                    "range": {
                                        "endtime": {
                                            "gte": starttime
                                        }
                                    }
                                },
				{
                                    "match": {
					    "dataset_type": "area_of_interest"
                                        }
                                }
                            ]
                        }
                    },
        {
        "filtered": {
            "query": {

              "bool": {
                "must": [
                  {
                    "match": {
                      "dataset_type": "area_of_interest"
                      }
                  },
                  {
                    "range": {
                      "starttime": {
                        "lte": endtime
                      }
                    }
                  }
                ]
              }
            },
            "filter": {
              "missing": {
                "field": "endtime"
              }
            }
          }
        },
        {
        "filtered": {
            "query": {

              "bool": {
                "must": [
                  {
                    "match": {
                      "dataset_type": "area_of_interest"
                      }
                  },
                  {
                    "range": {
                      "endtime": {
                        "gte": starttime
                      }
                    }
                  }
                ]
              }
            },
            "filter": {
              "missing": {
                "field": "starttime"
              }
            }
          }
        }
          
      ]
    }
  },
        "partial_fields" : {
            "partial" : {
                "include" : [ "id", "starttime", "endtime", "location", 
                              "metadata.user_tags", "metadata.priority" ]
            }
        }
    }

    # filter inactive
    hits = [i['fields']['partial'][0] for i in query_es(query) 
            if 'inactive' not in i['fields']['partial'][0].get('metadata', {}).get('user_tags', [])]
    #logger.info("hits: {}".format(json.dumps(hits, indent=2)))
    #logger.info("aois: {}".format(json.dumps([i['id'] for i in hits])))
    return hits

def get_orbit_file(orbit_dt, platform):
    logger.info("get_orbit_file : %s : %s" %(orbit_dt, platform))
    hits = util.query_orbit_file(orbit_dt, orbit_dt, platform)
    #logger.info("get_orbit_file : hits : \n%s\n" %hits)
    logger.info("get_orbit_file returns %s result " %len(hits))
    #return hits

    
    for hit in hits:
        metadata = hit["metadata"]     
        id = hit['id']     
        orbit_platform = metadata["platform"]   
        logger.info(orbit_platform)
        if orbit_platform == platform:
            url = metadata["context"]["localize_urls"][0]["url"]
            return True, id, url
    return False, None, None   
   


def query_aois_new(starttime, endtime):
    """Query ES for active AOIs that intersect starttime and endtime."""

    es_index = "grq_*_area_of_interest"
    query = {
        "query": {
            "bool": {
                "should": [
                    {
                        "bool": {
                            "must": [
                                {
                                    "range": {
                                        "starttime": {
                                            "lte": endtime
                                        }
                                    }
                                },
                                {
                                    "range": {
                                        "endtime": {
                                            "gte": starttime
                                        }
                                    }
                                },
				{
                                    "match": {
					    "dataset_type": "area_of_interest"
                                        }
                                },
                                {
                                    "match": {
                                            "metadata.tags": "standard_product"
                                        }
                                }
                            ],
                 	    "must_not": {
                		"term": {
                    		    "metadata.user_tags": "inactive"
                		}
            		    }
			  
                        }
                    },
        {
        "filtered": {
            "query": {

              "bool": {
                "must": [
                  {
                    "match": {
                      "dataset_type": "area_of_interest"
                      }
                  },
                  {
                    "range": {
                      "starttime": {
                        "lte": endtime
                      }
                    }
                  },
                  {
                      "match": {
                          "metadata.user_tags": "standard_product"
                      }
                  }
                ],
       	    	"must_not": {
                    "term": {
                        "metadata.user_tags": "inactive"
                    }
                }
              }
            },
            "filter": {
              "missing": {
                "field": "endtime"
              }
            }
          }
        },
        {
        "filtered": {
            "query": {

              "bool": {
                "must": [
                  {
                    "match": {
                      "dataset_type": "area_of_interest"
                      }
                  },
                  {
                    "range": {
                      "endtime": {
                        "gte": starttime
                      }
                     }
                  },
                  {
                      "match": {
                          "metadata.user_tags": "standard_product"
                      }
                  }
                ],
           	"must_not": {
                    "term": {
                        "metadata.user_tags": "inactive"
                    }
                }
              }
            },
            "filter": {
              "missing": {
                "field": "starttime"
              }
            }
          }
        }
          
      ]
    }
  },
        "partial_fields" : {
            "partial" : {
                "include" : [ "id", "starttime", "endtime", "location", 
                              "metadata.user_tags", "metadata.priority" ]
            }
        }
    }

    # filter inactive
    hits = [i['fields']['partial'][0] for i in query_es(query) 
            if 'inactive' not in i['fields']['partial'][0].get('metadata', {}).get('user_tags', [])]
    #logger.info("hits: {}".format(json.dumps(hits, indent=2)))
    #logger.info("aois: {}".format(json.dumps([i['id'] for i in hits])))
    return hits

def get_aois_by_id(aoi_list):
    aois = []
    for aoi in aoi_list:
        aoi_data = get_aoi_data_by_id(aoi)
        logger.info("aoi_data : %s" %aoi_data)
        if aoi_data and len(aoi_data)>0:
            logger.info("Adding data for aoi: %s" %aoi)
            aois.extend(aoi_data)
        else:
            logger.info("No data found for aoi: %s" %aoi)
    return aois


def get_aoi_data_by_id(aoi_id):
    es_index = "grq_*_area_of_interest"
    # query
    query = {
        "query":{
            "bool":{
                "must":[
                    { "term":{ "_id": aoi_id } },
                ]
            }
        },
         "partial_fields" : {
            "partial" : {
                "include" : [ "id", "starttime", "endtime", "location" ]
            }
        }
    }
     # filter inactive
    hits = [i['fields']['partial'][0] for i in query_es(query)]
    logger.info("hits: {}".format(json.dumps(hits, indent=2)))
    #logger.info("aois: {}".format(json.dumps([i['id'] for i in hits])))
    return hits


def get_dem_type(acq):
    dem_type = "SRTM+v3"
    if acq['city'] is not None and len(acq['city'])>0:
        if acq['city'][0]['country_name'] is not None and acq['city'][0]['country_name'].lower() == "united states":
            dem_type="Ned1"
    return dem_type

def getUpdatedTime(s, m):
    #date = dateutil.parser.parse(s, ignoretz=True)
    #new_date = s + timedelta(minutes = m)
    new_date = s + timedelta(minutes = m)
    return new_date

def get_time(t):

    logger.info("get_time(t) : %s" %t)
    t = parser.parse(t).strftime('%Y-%m-%dT%H:%M:%S')
    t1 = datetime.strptime(t, '%Y-%m-%dT%H:%M:%S')
    logger.info("returning : %s" %t1)
    return t1


def isTrackSelected(land, water, land_area, water_area):
    selected = False
    total_acq_land = 0

    for acq_land in land:
        total_acq_land+= acq_land

    if ((total_acq_land*100)/land)> 98:
        selected = True

    return selected
         


        
def print_groups(grouped_matched):
    for track in grouped_matched["grouped"]:
        logger.info("\nTrack : %s" %track)
        for day_dt in sorted(grouped_matched["grouped"][track], reverse=True):
            logger.info("\tDate : %s" %day_dt)
            for acq in grouped_matched["grouped"][track][day_dt]:

                logger.info("\t\t %s" %acq[0])



def get_covered_acquisitions_by_track_date(aoi, acqs, threshold_pixel, orbit_file, orbit_dir, platform, result_file, selected_track_list):
    #util.print_acquisitions(aoi['id'], util.create_acqs_from_metadata(acqs))


    logger.info("\nget_covered_acquisitions_by_track_date")
    
    logger.info("PROCESSING AOI : %s : %s" %(aoi['id'], aoi['location']))
    grouped_matched = util.group_acqs_by_track_date_from_metadata(acqs) #group_acqs_by_track(acqs)
    logger.info("grouped_matched Done")
    print_groups(grouped_matched)

    matched_ids = grouped_matched["acq_info"].keys()

    #logger.info("grouped_matched : %s" %grouped_matched)
    logger.info("matched_ids : %s" %matched_ids)
    logger.info("PLATFORM : %s" %platform)

    orbit_file = os.path.basename(orbit_file)
    mission = "S1A"
    if platform == "Sentinel-1B":
        mission = "S1B"


    selected_track_acqs = {}


    logger.info("Tracks to process : %s" %grouped_matched["grouped"])
    for track in grouped_matched["grouped"]:
        logger.info("get_covered_acquisitions_by_track_date : Processing track : %s" %track)
        if len(selected_track_list)>0:
            if int(track) not in selected_track_list:
                logger.info("%s not in selected_track_list %s. So skipping this track" %(track, selected_track_list))
                continue
        selected_track_dt_acqs = {}
        for track_dt in grouped_matched["grouped"][track]:
            #water_mask_test1(track, orbit_or_track_dt, acq_info, grouped_matched_orbit_number,  aoi_location, aoi_id,  orbit_file = None)
            selected, result = gtUtil.water_mask_check(track, track_dt, grouped_matched["acq_info"], grouped_matched["grouped"][track][track_dt],  aoi['location'], aoi['id'], threshold_pixel, mission, orbit_file, orbit_dir)
            with open(result_file, 'a') as fo:
                cw = csv.writer(fo, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                cw.writerow([result['dt'], result['track'],result['Track_POEORB_Land'] , result['ACQ_Union_POEORB_Land'], result['acq_union_land_area'], result['res'], result['WATER_MASK_PASSED'], result['master_ipf_count'], result['slave_ipf_count'],result['matched'], result['BL_PASSED'], result['candidate_pairs'], result['Track_AOI_Intersection'], result['ACQ_POEORB_AOI_Intersection'], result['acq_union_aoi_intersection'] ])

            if selected:
                logger.info("SELECTED : aoi : %s track : %s  track_dt : %s" %(aoi['id'], track, track_dt))
                selected_acqs = []
                for acq_id in grouped_matched["grouped"][track][track_dt]:
                    acq = grouped_matched["acq_info"][acq_id]

                        #acq.pv = pv #util.get_processing_version(acq.identifier)
                        #util.update_grq(acq_id, acq.pv)
                    logger.info("APPENDING : %s" %acq_id)
                    selected_acqs.append(acq)
                selected_track_dt_acqs[track_dt] = selected_acqs
        selected_track_acqs[track] = selected_track_dt_acqs



    #exit (0)
    logger.info("get_covered_acquisitions_by_track_date returns : %s" %selected_track_acqs)
    return selected_track_acqs
 
def get_covered_acquisitions(aoi, acqs, orbit_file):
    #util.print_acquisitions(aoi['id'], util.create_acqs_from_metadata(acqs))

    logger.info("AOI : %s" %aoi['location'])
    grouped_matched = util.group_acqs_by_orbit_number_from_metadata(acqs) #group_acqs_by_track(acqs)
    matched_ids = grouped_matched["acq_info"].keys()
           
    #logger.info("grouped_matched : %s" %grouped_matched)
    logger.info("matched_ids : %s" %matched_ids)


    selected_track_acqs = {}

    

    for track in grouped_matched["grouped"]:
        selected_orbitnumber_acqs = {}
        for orbitnumber in grouped_matched["grouped"][track]:
            selected = gtUtil.water_mask_check(track, orbitnumber, grouped_matched["acq_info"], grouped_matched["grouped"][track][orbitnumber],  aoi['location'], aoi['id'], threshold_pixel, orbit_file)
            if selected:
                logger.info("SELECTED")
                selected_acqs = []
                for pv in grouped_matched["grouped"][track][orbitnumber]:
                    for acq_id in grouped_matched["grouped"][track][orbitnumber][pv]:
                        acq = grouped_matched["acq_info"][acq_id]
                        
                        if not acq.pv:
                            acq.pv = pv #util.get_processing_version(acq.identifier)
                            #util.update_grq(acq_id, acq.pv)
                        logger.info("APPENDING : %s" %acq_id)
                        selected_acqs.append(acq)
                selected_orbitnumber_acqs[orbitnumber] = selected_acqs
        selected_track_acqs[track] = selected_orbitnumber_acqs     

        

    #exit (0)

    return selected_track_acqs

def query_aoi_acquisitions(starttime, endtime, platform, orbit_file, orbit_dir, threshold_pixel, acquisition_version, selected_track_list, selected_aoi_list):
    """Query ES for active AOIs that intersect starttime and endtime and 
       find acquisitions that intersect the AOI polygon for the platform."""
    #aoi_acq = {}
    orbit_aoi_data = {}
    es_index = "grq_*_*acquisition*"
    es_index = "grq_%s_acquisition-s1-iw_slc/acquisition-S1-IW_SLC/" %(acquisition_version)
    logger.info("query_aoi_acquisitions : es_index : %s" %es_index)
    aois = None
    if len(selected_aoi_list)>0:
        aois = get_aois_by_id(selected_aoi_list)
    else:
        aois = query_aois_new(starttime, endtime)

    logger.info("No of AOIs : %s " %len(aois))
    logger.info("aois : %s" %aois)

    if not aois or len(aois) <=0:
        logger.info("Existing as NO AOI Found")
        sys.exit(0)
    for aoi in aois:
        logger.info("aoi: {}".format(aoi['id']))
        query = {
            "query": {
                "filtered": {
                    "query": {
                        "bool": {
                            "must": [
                                {
                                    "term": {
                                        "dataset_type.raw": "acquisition"
                                    }
                                },
                                 {
                                    "term": {
                                        "version.raw": acquisition_version
                                    }
                                },
                                {
                                    "term": {
                                        "metadata.platform.raw": platform
                                    }
                                },
                                {
                                    "range": {
                                        "starttime": {
                                            "lte": endtime
                                        }
                                    }
                                },
                                {
                                    "range": {
                                        "endtime": {
                                            "gte": starttime
                                        }
                                    }
                                }
                            ]
                        }
                    },
                    "filter": {
                        "geo_shape": {  
                            "location": {
                                "shape": aoi['location']
                            }
                        }
                    }
                }
            },
            "partial_fields" : {
                "partial" : {
                    "include" : [ "id", "dataset_type", "dataset", "metadata", "city", "continent", "starttime", "endtime"]
                }
            }
        }
        logger.info(query)
        acqs = [i['fields']['partial'][0] for i in query_es(query, es_index)]
        logger.info("Found {} acqs for {}: {}".format(len(acqs), aoi['id'],
                    json.dumps([i['id'] for i in acqs], indent=2)))

        #logger.info("ALL ACQ of AOI : \n%s" %acqs)
        if len(acqs) <=0:
            logger.info("Excluding AOI %s as no acquisitions there" %aoi['id'])
        selected_track_acqs = {}
        result_file = "RESULT_SUMMARY_%s.csv" %aoi['id']
        with open(result_file, 'w') as fo:
            cw = csv.writer(fo, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            cw.writerow(["Date","Track","Track_PEORB_Land","ACQ_Union_POEORB_Land", "ACQ_Union_Land", "RES", "WATER_MASK_PASSED", "Master_Ipf_Count", "Slave_Ipf_Count", "MATCHED", "BL_PASSED", "Candidate_Pairs", "Track_AOI_Intersection", "ACQ_POEORB_AOI_Intersection", "ACQ_AOI_Ingtersection"])

        selected_track_acqs = get_covered_acquisitions_by_track_date(aoi, acqs, threshold_pixel, orbit_file, orbit_dir, platform, result_file, selected_track_list)

        if len(selected_track_acqs.keys())==0:
            logger.info("Nothing selected from AOI %s " %aoi['id'])
            continue

        #for acq in acqs:
        aoi_data = {}
        aoi_priority = aoi.get('metadata', {}).get('priority', 0)
        # ensure highest priority is assigned if multiple AOIs resolve the acquisition
        #if acq['id'] in acq_info and acq_info[acq['id']].get('priority', 0) > aoi_priority:
            #continue
        aoi_data['aoi_id'] = aoi['id']
        aoi_data['aoi_location'] =  aoi['location']
        aoi_data['priority'] = aoi_priority
        aoi_data['selected_track_acqs'] = selected_track_acqs
        orbit_aoi_data[aoi['id']] = aoi_data
        #acq_info[aoi_data['id']] = acq
	#aoi_acq[aoi] = acq_info 
        #logger.info("Acquistions to localize: {}".format(json.dumps(acq_info, indent=2)))
    if len(orbit_aoi_data.keys())<=0:
        logger.info("Existing as NOTHING selected for any aois")
        sys.exit(0)
    return orbit_aoi_data
    

def resolve_s1_slc(identifier, download_url, project):
    """Resolve S1 SLC using ASF datapool (ASF or NGAP). Fallback to ESA."""

    # determine best url and corresponding queue
    vertex_url = "https://datapool.asf.alaska.edu/SLC/SA/{}.zip".format(identifier)
    r = requests.head(vertex_url, allow_redirects=True)
    if r.status_code == 403:
        url = r.url
        queue = "{}-job_worker-small".format(project)
    elif r.status_code == 404:
        url = download_url
        queue = "factotum-job_worker-scihub_throttled"
    else:
        raise RuntimeError("Got status code {} from {}: {}".format(r.status_code, vertex_url, r.url))
    return url, queue


class DatasetExists(Exception):
    """Exception class for existing dataset."""
    pass

def get_temporal_baseline(ctx):
    temporalBaseline = 24
    if 'temporalBaseline' in ctx:
        temporalBaseline = int(ctx['temporalBaseline'])
    return temporalBaseline
   

def resolve_s1_slc(identifier, download_url, project):
    """Resolve S1 SLC using ASF datapool (ASF or NGAP). Fallback to ESA."""

    # determine best url and corresponding queue
    vertex_url = "https://datapool.asf.alaska.edu/SLC/SA/{}.zip".format(identifier)
    r = requests.head(vertex_url, allow_redirects=True)
    if r.status_code == 403:
        url = r.url
        queue = "{}-job_worker-small".format(project)
    elif r.status_code == 404:
        url = download_url
        queue = "factotum-job_worker-scihub_throttled"
    else:
        raise RuntimeError("Got status code {} from {}: {}".format(r.status_code, vertex_url, r.url))
    return url, queue


class DatasetExists(Exception):
    """Exception class for existing dataset."""
    pass

def get_temporal_baseline(ctx):
    temporalBaseline = 24
    if 'temporalBaseline' in ctx:
        temporalBaseline = int(ctx['temporalBaseline'])
    return temporalBaseline

def resolve_aoi_acqs(ctx_file):
    """Resolve best URL from acquisitions from AOIs."""

    # read in context
    with open(ctx_file) as f:
        ctx = json.load(f)

    project = ctx['project']
    logger.info("PROJECT : %s" %project)
    priority = int(ctx["job_priority"])
    minMatch = int(ctx["minMatch"])
    dataset_version = ctx["dataset_version"] 
    acquisition_version = ctx["acquisition_version"]  
    threshold_pixel = int(ctx["threshold_pixel"])
    job_type, job_version = ctx['job_specification']['id'].split(':')

    selected_track_list = []

    try:
        if "track_numbers" in ctx and ctx["track_numbers"] is not None:
            track_numbers = ctx["track_numbers"].strip()
            if track_numbers:
                track_numbers_list = track_numbers.split(',')
                for tn in track_numbers_list:
                    selected_track_list.append(int(tn))
    except:
        pass
    selected_aoi_list = []

    try:
        if "aoi_name" in ctx and ctx["aoi_name"] is not None:
            aois = ctx["aoi_name"].strip()
            logger.info("passed aoi: %s" %aois)
            if aois:
                aoi_list = aois.split(',')
                logger.info(aoi_list)
                for aoi in aoi_list:
                    selected_aoi_list.append(aoi.strip())
    except:
        pass
    selected_aoi_list = list(set(selected_aoi_list))
    logger.info("selected_aoi_list : %s" %selected_aoi_list)
    logger.info("selected_track_list : %s" %selected_track_list)

    #Find Orbit File Info
    orbit_file = None
    orbit_file_dir =os.path.basename(ctx["localize_urls"][0]["url"])
    for file in os.listdir(orbit_file_dir):
        if file.endswith(".EOF"):
            orbit_file = os.path.join(orbit_file_dir, file)

    if not orbit_file:
        raise RuntimeError("Orbit File NOT Found")
    else:
        logger.info("Orbit File : %s " %orbit_file)


    orbit_aoi_data = query_aoi_acquisitions(ctx['starttime'], ctx['endtime'], ctx['platform'], orbit_file, orbit_file_dir, threshold_pixel, acquisition_version, selected_track_list, selected_aoi_list)
    #osaka.main.get("http://aux.sentinel1.eo.esa.int/POEORB/2018/09/15/S1A_OPER_AUX_POEORB_OPOD_20180915T120754_V20180825T225942_20180827T005942.EOF")
    #logger.info(orbit_aoi_data)
    #exit(0)
    
    # build args
    #queue = ctx["recommended-queues"][0]
    queue = "system-jobs-queue"
    singlesceneOnly = True
    precise_orbit_only = True
    
    job_data = {}
    job_data["project"] = project
    '''
    job_data["spyddder_extract_version"] = spyddder_extract_version
    job_data["standard_product_ifg_version"] = standard_product_ifg_version
    job_data["acquisition_localizer_version"] = acquisition_localizer_version
    job_data["standard_product_localizer_version"] = standard_product_localizer_version
    '''

    job_data["job_type"] = job_type
    job_data["job_version"] = job_version
    job_data["job_priority"] = ctx['job_priority']
    job_data['orbit_file'] = orbit_file 
    job_data['minMatch'] = minMatch
    job_data['threshold_pixel'] = threshold_pixel
    job_data["acquisition_version"] = acquisition_version
    job_data["selected_track_list"] = selected_track_list

    
    orbit_data = {}
    orbit_data['starttime'] = ctx['starttime']
    orbit_data['endtime'] = ctx['endtime']
    orbit_data['platform'] = ctx['platform']
    orbit_data['orbit_file'] = orbit_file  

    orbit_acq_selections = {}
    orbit_acq_selections["job_data"] = job_data
    orbit_acq_selections["orbit_aoi_data"] = orbit_aoi_data
    orbit_acq_selections["orbit_data"] = orbit_data

    return orbit_acq_selections


    '''
    acquisitions = []
    acquisition_array =[]

    for id in sorted(acq_info):
        acq = acq_info[id]
        aoi = acq['aoi']
        pv = None
        if "processing_version" in  acq['metadata']:
            pv = acq['metadata']['processing_version']
            pv2 = get_processing_version(acq['metadata']['identifier'])
            logger.info("pv : %s, pv2 : %s" %(pv, pv2))
        else:
            pv = get_processing_version(acq['metadata']['identifier'])
        logging.info("\n\nPrinting AOI : %s, Acq : %s" %(aoi, id))
            #print(acq)

    	#job_type = "job-standard_product_localizer:{}".format(standard_product_localizer_version)
       
	    #return id, project, spyddder_extract_version, aoi, priority, queue


        acq_data = {
            "acq_id" : id,
            "project" : project,
            #"identifier" : acq['metadata']['identifier'],
	    "spyddder_extract_version" : spyddder_extract_version,
	    "standard_product_ifg_version" : standard_product_ifg_version,
	    "acquisition_localizer_version" : acquisition_localizer_version,
	    "standard_product_localizer_version" : standard_product_localizer_version,
  	    #"job_type" : job_type, 
	    #"job_version" : job_version,
	    "job_priority" : ctx['job_priority']
		
        } 

        #logger.info("\n\nacq data for :%s :\n%s" %(id, acq_data))
        acquisition_array.append(acq_data)

    acquisitions.append(acquisition_array)

    logging.info("acquisition_array length : %s" %acquisition_array)
    #return acquisitions
    return acquisition_array

   
        logging.info("acq identifier : %s " %identifier)
	logging.info("acq city : %s " %acq['city'])
        logging.info("dem_type : %s" %dem_type)
	logging.info("query : %s" %query)
	logging.info("bbox : %s" %bbox)
        logging.info("ipf : %s" %ipf)
	
        acquisition_array.append([project, True, bbox, dataset, identifier, download_url, dataset_type, ipf, archive_filename, query, aoi, dem_type, spyddder_extract_version, standard_product_version, queue, job_priority, preReferencePairDirection, postReferencePairDirection, temporalBaseline, singlesceneOnly, precise_orbit_only])
        
        return project, True, bbox, dataset, identifier, download_url, dataset_type, ipf, archive_filename, query, aoi, dem_type, spyddder_extract_version, standard_product_version, queue, job_priority, preReferencePairDirection, postReferencePairDirection, temporalBaseline, singlesceneOnly, precise_orbit_only
        exit(0)
        #job = resolve_hysds_job(job_type, queue, priority=acq['priority'], params=params, job_name="%s-%s-%s" % (job_type, aoi, prod_name))

        #job_id = submit_hysds_job(job)

    '''

def main():

    # read in _context.json
    context_file = os.path.abspath("_context.json")
    if not os.path.exists(context_file):
        raise RuntimeError("Context file doesn't exist.")
    
    resolve_aoi_acqs(context_file)

if __name__ == "__main__":
    sys.exit(main())
