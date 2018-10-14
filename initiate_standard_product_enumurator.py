#!/usr/bin/env python3

import os, sys, time, json, requests, logging
import orbit_acquisition_selector
import standard_product_enumurator
import publish_initiator

def get_candidate_pair_list():
    candidate_pair_list = []
    candidate_pair = {}
    candidate_pair["master_acqs"]=["acquisition-S1A_IW_SLC__1SDV_20180913T104147_20180913T104217_023678_0294B6_D605"]
    candidate_pair["slave_acqs"]=["acquisition-S1A_IW_SLC__1SSV_20150929T104112_20150929T104142_007928_00B136_DE3B", "acquisition-S1A_IW_SLC__1SSV_20150929T104140_20150929T104158_007928_00B136_04F4"]
    candidate_pair["starttime"] = "2018-08-20 10:41:18"
    candidate_pair["endtime"]= "2018-09-13 10:42:17"
    candidate_pair["union_geojson"] = {'coordinates': [[[114.340401, -8.231028], [114.34057893068714, -8.230987954571027], [113.975792, -6.564369], [116.197609, -6.068401], [116.19765497529309, -6.068605119889187], [116.197884, -6.068554], [116.5997637151398, -7.852971167971122], [116.98259, -9.522411], [114.739487, -10.031385], [114.37047609067962, -8.36670276691912], [114.370293, -8.366744], [114.35522339174503, -8.297894782435131], [114.340401, -8.231028]]], 'type': 'Polygon'}
    candidate_pair_list.append(candidate_pair)
    return candidate_pair_list

def main():

    
    context_file = os.path.abspath("_context.json")
    if not os.path.exists(context_file):
        raise(RuntimeError("Context file doesn't exist."))
    
    orbit_acq_selections = orbit_acquisition_selector.resolve_aoi_acqs(context_file)
    #print("\n\norbit_acq_selections:\n%s" %orbit_acq_selections)
    candidate_pair_list = standard_product_enumurator.enumerate_acquisations(orbit_acq_selections)
    #candidate_pair_list = get_candidate_pair_list()
    for candidate_pair in candidate_pair_list:
        print("\n\nMaster ACQS : ")
        for acq in  candidate_pair["master_acqs"]:
            print(acq)

        print("\n\nSLAVE ACQS : ")
        for acq in  candidate_pair["slave_acqs"]:
            print(acq)
        print("\nunion_geojson : %s" %candidate_pair["union_geojson"]) 
        print("\nstarttime : %s" %candidate_pair["starttime"])
        print("\nendtime : %s" %candidate_pair["endtime"])


    job_data = orbit_acq_selections["job_data"]
    #job_data = {}
    publish_initiator.publish_initiator(candidate_pair_list, job_data)

if __name__ == "__main__":
    sys.exit(main())