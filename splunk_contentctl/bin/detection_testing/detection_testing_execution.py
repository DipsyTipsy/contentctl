import copy
import os
import random
import shutil
import docker
import sys

from collections import OrderedDict
from datetime import datetime
from posixpath import basename
from tempfile import mkdtemp
from timeit import default_timer as timer
from typing import Union
from urllib.parse import urlparse
import signal
import pathlib

import requests



from bin.detection_testing.modules import container_manager, new_arguments2, test_driver, validate_args, utils, github_service, constants
from bin.objects.test_config import TestConfig
from splunk_contentctl.bin.detection_testing.modules.github_service import GithubService

SPLUNK_CONTAINER_APPS_DIR = "/opt/splunk/etc/apps"
index_file_local_path = "bin/detection_testing/indexes.conf.tar"
index_file_container_path = os.path.join(SPLUNK_CONTAINER_APPS_DIR, "search")

# Should be the last one we copy.
datamodel_file_local_path = "bin/detection_testing/datamodels.conf.tar"
datamodel_file_container_path = os.path.join(
    SPLUNK_CONTAINER_APPS_DIR, "Splunk_SA_CIM")


authorizations_file_local_path = "bin/detection_testing/authorize.conf.tar"
authorizations_file_container_path = "/opt/splunk/etc/system/local"

CONTAINER_APP_DIRECTORY = "apps"

MAX_RECOMMENDED_CONTAINERS_BEFORE_WARNING = 2





def copy_local_apps_to_directory(apps: dict[str, dict], splunkbase_username:tuple[str,None] = None, splunkbase_password:tuple[str,None] = None, mock:bool = False, target_directory:str = "apps") -> str:
    if mock is True:
        target_directory = os.path.join("prior_config", target_directory)
        
        # Remove the apps directory or the prior config directory.  If it's just an apps directory, then we don't want
        #to remove that.
        shutil.rmtree(target_directory, ignore_errors=True)
    try:
        # Make sure the directory exists.  If it already did, that's okay. Don't delete anything from it
        # We want to re-use previously downloaded apps
        os.makedirs(target_directory, exist_ok = True)
        
    except Exception as e:
        raise(Exception(f"Some error occured when trying to make the {target_directory}: [{str(e)}]"))

    
    for key, item in apps.items():

        # These apps are URLs that will be passed.  The apps will be downloaded and installed by the container
        # # Get the file from an http source
        splunkbase_info = True if ('app_number' in item and item['app_number'] is not None and 
                                  'app_version' in item and item['app_version'] is not None) else False
        splunkbase_creds = True if (splunkbase_username is not None and 
                                   splunkbase_password is not None) else False
        can_download_from_splunkbase = splunkbase_info and splunkbase_creds

        

        #local apps can either have a local_path or an http_path
        if 'local_path' in item:
            source_path = os.path.abspath(os.path.expanduser(item['local_path']))
            base_name = os.path.basename(source_path)
            dest_path = os.path.join(target_directory, base_name)
            try:
                print(f"copying {os.path.relpath(source_path)} to {os.path.relpath(dest_path)}")
                shutil.copy(source_path, dest_path)
                item['local_path'] = dest_path
            except shutil.SameFileError as e:
                # Same file, not a real error.  The copy just doesn't happen
                print("err:%s" % (str(e)))
                pass
            except Exception as e:
                print("Error copying ESCU Package [%s] to [%s]: [%s].\n\tQuitting..." % (
                    source_path, dest_path, str(e)), file=sys.stderr)
                sys.exit(1)

        
        elif can_download_from_splunkbase is True:
            #Don't do anything, this will be downloaded from splunkbase
            pass
        elif splunkbase_info is True and splunkbase_creds is False and mock is True:
            #Don't need to do anything, when this actually runs the apps will be downloaded from Splunkbase
            #There is another opportunity to provide the creds then
            pass
        elif 'http_path' in item and can_download_from_splunkbase is False:
            http_path = item['http_path']
            try:
                url_parse_obj = urlparse(http_path)
                path_after_host = url_parse_obj[2].rstrip('/') #removes / at the end, if applicable
                base_name = path_after_host.rpartition('/')[-1] #just get the file name
                dest_path = os.path.join(target_directory, base_name) #write the whole path
                utils.download_file_from_http(http_path, dest_path, verbose_print=True)
                #we need to update the local path because this is used to copy it into the container later
                item['local_path'] = dest_path
                #Remove the HTTP Path, we will use the local_path instead
            except Exception as e:
                print("Error trying to download %s @ %s: [%s].  This app is required.\n\tQuitting..."%(key, http_path, str(e)),file=sys.stderr)
                sys.exit(1)

        elif splunkbase_info is False:
            print(f"Error - trying to install an app [{key}] that does not have 'local_path', 'http_path', "
                   "or 'app_version' and 'app_number' for installing from Splunkbase.\n\tQuitting...")
            sys.exit(1)
    return target_directory





def finish_mock(settings: dict, detections: list[test_driver.Detection], output_file_template: str = "prior_config/config_tests_%d.json")->bool:
    num_containers = settings['num_containers']

    #convert the list of Detection objects into a list of filename strings
    detection_filesnames = [str(d.detectionFile.path) for d in detections]
    
    for output_file_index in range(0, num_containers):
        fname = output_file_template % (output_file_index)

        # Get the n'th detection for this file
        detection_tests = detection_filesnames[output_file_index::num_containers]
        normalized_detection_names = []
        # Normalize the test filename to the name of the detection instead.
        # These are what we should write to the file
        for d in detection_tests:
            filename = os.path.basename(d)
            filename = filename.replace(".test.yml", ".yml")
            #leading = os.path.split(d)[0]
            #leading = leading.replace()
            #new_name = os.path.join(
            #    "security_content", leading, filename)
            #normalized_detection_names.append(new_name)
            normalized_detection_names.append(d.replace(".test.yml", ".yml").replace("tests/", "detections/"))

        # Generate an appropriate config file for this test
        mock_settings = copy.deepcopy(settings)
        # This may be able to support as many as 2 for GitHub Actions...
        # we will have to determine in testing.
        mock_settings['num_containers'] = 1

        # Must be selected since we are passing in a list of detections
        mock_settings['mode'] = 'selected'

        # Pass in the list of detections to run
        mock_settings['detections_list'] = normalized_detection_names

        # We want to persist security content and run with the escu package that we created.
        #Note that if we haven't checked this out yet, we will check it out for you.
        mock_settings['persist_security_content'] = True

        mock_settings['mock'] = False

        # Make sure that it still validates after all of the changes

        try:
            with open(fname, 'w') as outfile:
                validated_settings, b = validate_args.validate_and_write(configuration=mock_settings, output_file = outfile, strip_credentials=True)
                if validated_settings is None:
                    print(
                        "There was an error validating the updated mock settings.\n\tQuitting...", file=sys.stderr)
                    return False

        except Exception as e:
            print("Error writing config file %s: [%s]\n\tQuitting..." % (
                fname, str(e)), file=sys.stderr)
            return False

    return True



def ensure_docker_is_avaliable(config: TestConfig):
    #If this is a mock, then docker doesn't need to be running so we will
    #not check for it
    if config.mock == True:
        return

    #This is a real test run, so ensure that docker is running on this machine
    try:
        docker.client.from_env()
    except Exception as e:
        raise(Exception(f"Error, failed to get docker client.  Is Docker Installed and Running? Error:\n\t{str(e)}"))

    


def main(config: TestConfig):
    #Disable insecure warnings.  We make a number of HTTPS requests to Splunk
    #docker containers that we've set up.  Without this line, we get an 
    #insecure warning every time due to invalid cert.
    requests.packages.urllib3.disable_warnings()


    ensure_docker_is_avaliable(config)
    
    
    #Get a handle to the git repo
    github_service = GithubService(config.repo_path)


    detections_to_test = github_service.get_detections_to_test(config.mode, config.detections_list)
    
    try:
        all_detections = github_service.detections_to_test(settings['mode'], detections_list=settings['detections_list'])
        #all_test_files = github_service.get_test_files(settings['mode'],
        #                                            settings['folders'],
        #                                            settings['types'],
        #                                            settings['detections_list'])
        
        #We randomly shuffle this because there are likely patterns in searches.  For example,
        #cloud/endpoint/network likely have different impacts on the system.  By shuffling,
        #we spread out this load on a single computer, but also spread it in case
        #we are running on GitHub Actions against multiple machines.  Hopefully, this
        #will reduce that chnaces the some machines run and complete quickly while
        #others take a long time.
        random.shuffle(all_detections)        
    
    
    

    except Exception as e:
        print("Error getting test files:\n%s"%(str(e)), file=sys.stderr)
        print("\tQuitting...", file=sys.stderr)
        sys.exit(1)
    
    
    print("***This run will test [%d] detections!***"%(len(all_detections)))
    


    


    # Check to see if we want to install ESCU and whether it was preeviously generated and we should use that file
    if constants.ES_APP_NAME in settings['apps'] and settings['apps'][constants.ES_APP_NAME]['local_path'] is not None:
        # Using a pregenerated ESCU, no need to build it
        pass

    elif constants.ES_APP_NAME not in settings['apps']:
        print(f"{constants.ES_APP_NAME} was not found in {settings['apps'].keys()}.  We assume this is an error and shut down.\n\t"
              "Quitting...", file=sys.stderr)
        sys.exit(1)
    else:
        # Generate the ESCU package from this branch.
        
        settings['apps']['SPLUNK_ES_CONTENT_UPDATE']['local_path'] = "build/my_app.tar.gz"
        

    # Copy all the apps, to include ESCU (whether pregenerated or just generated)
    try:
        relative_app_path = copy_local_apps_to_directory(settings['apps'], 
                                     splunkbase_username = settings['splunkbase_username'], 
                                     splunkbase_password = settings['splunkbase_password'], 
                                     mock=settings['mock'], target_directory = CONTAINER_APP_DIRECTORY)
        
        mounts = [{"local_path": os.path.abspath(relative_app_path),
                    "container_path": "/tmp/apps", "type": "bind", "read_only": True}]
    except Exception as e:
        print(f"Error occurred when copying apps to app folder: [{str(e)}]\n\tQuitting...", file=sys.stderr)
        sys.exit(1)


    # If this is a mock run, finish it now
    if settings['mock']:
        #The function below 
        if finish_mock(settings, all_detections):
            # mock was successful!
            print("Mock successful!  Manifests generated!")
            sys.exit(0)
        else:
            print("There was an unrecoverage error during the mock.\n\tQuitting...",file=sys.stderr)
            sys.exit(1)



    #Add some files that always need to be copied to to container to set up indexes and datamodels.
    files_to_copy_to_container = OrderedDict()
    files_to_copy_to_container["INDEXES"] = {
        "local_file_path": index_file_local_path, "container_file_path": index_file_container_path}
    files_to_copy_to_container["DATAMODELS"] = {
        "local_file_path": datamodel_file_local_path, "container_file_path": datamodel_file_container_path}
    files_to_copy_to_container["AUTHORIZATIONS"] = {
        "local_file_path": authorizations_file_local_path, "container_file_path": authorizations_file_container_path}
    

    
    def shutdown_signal_handler_setup(sig, frame):
        
        print(f"Signal {sig} received... stopping all [{settings['num_containers']}] containers and shutting down...")
        shutdown_client = docker.client.from_env()
        errorCount = 0
        for container_number in range(settings['num_containers']):
            container_name = settings['local_base_container_name']%container_number
            print(f"Shutting down {container_name}...", file=sys.stderr, end='')
            sys.stdout.flush()
            try:
                container = shutdown_client.containers.get(container_name)
                #Note that stopping does not remove any of the volumes or logs,
                #so stopping can be useful if we want to debug any container failure 
                container.stop(timeout=10)
                print("done", file=sys.stderr)
            except Exception as e:
                print(f"Error trying to shut down {container_name}. It may have already shut down.  Stop it youself with 'docker containter stop {container_name}", sys.stderr)
                errorCount += 1
        if errorCount == 0:
            print("All containers shut down successfully", file=sys.stderr)        
        else:
            print(f"{errorCount} containers may still be running. Find out what is running with:\n\t'docker container ls'\nand shut them down with\n\t'docker container stop CONTAINER_NAME' ", file=sys.stderr)
        
        print("Quitting...",file=sys.stderr)
        #We must use os._exit(1) because sys.exit(1) actually generates an exception which can be caught! And then we don't Quit!
        os._exit(1)
        

            

    #Setup requires a different teardown handler than during execution
    signal.signal(signal.SIGINT, shutdown_signal_handler_setup)

    try:
        cm = container_manager.ContainerManager(all_detections,
                                                FULL_DOCKER_HUB_CONTAINER_NAME,
                                                settings['local_base_container_name'],
                                                settings['num_containers'],
                                                settings['apps'],
                                                settings['branch'],
                                                settings['commit_hash'],
                                                reproduce_test_config,
                                                files_to_copy_to_container=files_to_copy_to_container,
                                                web_port_start=8000,
                                                management_port_start=8089,
                                                hec_port_start=8088,
                                                mounts=mounts,
                                                show_container_password=settings['show_splunk_app_password'],
                                                container_password=settings['splunk_app_password'],
                                                splunkbase_username=settings['splunkbase_username'],
                                                splunkbase_password=settings['splunkbase_password'],
                                                reuse_image=settings['reuse_image'],
                                                interactive_failure=not settings['no_interactive_failure'],
                                                interactive=settings['interactive'])
    except Exception as e:
        print("Error - unrecoverable error trying to set up the containers: [%s].\n\tQuitting..."%(str(e)),file=sys.stderr)
        sys.exit(1)

    def shutdown_signal_handler_execution(sig, frame):
        #Set that a container has failed which will gracefully stop the other containers.
        #This way we get our full cleanup routine, too!
        print("Got a signal to shut down. Shutting down all containers, please wait...", file=sys.stderr)
        cm.synchronization_object.containerFailure()
    
    #Update the signal handler

    signal.signal(signal.SIGINT, shutdown_signal_handler_execution)
    try:
        result = cm.run_test()
    except Exception as e:
        print("Error - there was an error running the tests: [%s]\n\tQuitting..."%(str(e)),file=sys.stderr)
        sys.exit(1)


    cm.synchronization_object.resultsManager.generate_results_file(pathlib.Path("summary.json"))

    #github_service.update_and_commit_passed_tests(cm.synchronization_object.successes)
    

    #Return code indicates whether testing succeeded and all tests were run.
    #It does NOT indicate that all tests passed!
    if result is True:
        print("Test Execution Successful")
        sys.exit(0)
    else:
        print("Test Execution Failed - review the logs for more details")
        #Because one or more of the threads could be stuck in a certain setup loop, like
        #trying to copy files to a containers (which igonores errors), we must os._exit
        #instead of sys.exit
        os._exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])

