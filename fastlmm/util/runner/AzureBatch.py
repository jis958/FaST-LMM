import logging
import datetime
import fastlmm.util.runner.azurehelper as commonhelpers #!!!cmk is this the best way to include the code from the Azure python sample's common.helper.py?
import os
from fastlmm.util.runner import *
try:
    import dill as pickle
except:
    logging.warning("Can't import dill, so won't be able to clusterize lambda expressions. If you try, you'll get this error 'Can't pickle <type 'function'>: attribute lookup __builtin__.function failed'")
    import cPickle as pickle

try:
    import azure.batch.batch_service_client as batch 
    import azure.batch.batch_auth as batchauth 
    import azure.batch.models as batchmodels
    import azure.storage.blob as azureblob
    from fastlmm.util.runner.blobxfer import run_command_string as blobxfer #https://pypi.io/project/blobxfer/

except Exception as exp:
    logging.warning(exp)
    pass

class AzureBatch: # implements IRunner
    def __init__(self, taskcount, mkl_num_threads = None, logging_handler=logging.StreamHandler(sys.stdout)):
        logger = logging.getLogger() #!!!cmk similar code elsewhere
        if not logger.handlers:
            logger.setLevel(logging.INFO)
        for h in list(logger.handlers):
            logger.removeHandler(h)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(logging_handler)

        self.taskcount = taskcount
        self.mkl_num_threads = mkl_num_threads

    def run(self, distributable):
        JustCheckExists().input(distributable) #!!!cmk move input files
        batch_service_url, batch_account, batch_key, storage_account, storage_key = [s.strip() for s in open(os.path.expanduser("~")+"/azurebatch/cred.txt").xreadlines()] #!!!cmk make this a param????

        ####################################################
        # Pickle the thing-to-run
        ####################################################
        run_dir_rel = os.path.join("runs",util.datestamp(appendrandom=True))
        util.create_directory_if_necessary(run_dir_rel, isfile=False)
        distributablep_filename = os.path.join(run_dir_rel, "distributable.p")
        with open(distributablep_filename, mode='wb') as f:
            pickle.dump(distributable, f, pickle.HIGHEST_PROTOCOL)

        ####################################################
        # Create the batch program to run
        ####################################################
        dist_filename = os.path.join(run_dir_rel, "dist.bat")
        with open(dist_filename, mode='w') as f:
            f.write(r"""c:\Anaconda2\python.exe blobxfer.py --delete --storageaccountkey {2} --download {3} pp0 c:\user\tasks\workitems\pps\pp0 --remoteresource .
set pythonpath=c:\user\tasks\workitems\pps\pp0
c:\Anaconda2\python.exe c:\Anaconda2\Lib\site-packages\fastlmm\util\distributable.py distributable.p LocalInParts(%1,{0},mkl_num_threads={1})
            """
            .format(
                self.taskcount,                         #0
                self.mkl_num_threads,                   #1
                storage_key,                            #2 #!!!cmk use the URL instead of the key
                storage_account,                        #3
            ))#!!!cmk need multiple blobxfer lines

        ####################################################
        # Upload the thing-to-run to a blob and the blobxfer program
        ####################################################
        block_blob_client = azureblob.BlockBlobService(account_name=storage_account,account_key=storage_key)
        block_blob_client.create_container('application', fail_on_exist=False) #!!!cmk subfolders for each run
        distributablep_url = commonhelpers.upload_blob_and_create_sas(block_blob_client, 'application', "distributable.p", distributablep_filename, datetime.datetime.utcnow() + datetime.timedelta(hours=1))
        blobxfer_fn = os.path.join(os.path.dirname(__file__),"blobxfer.py")
        blobxfer_url = commonhelpers.upload_blob_and_create_sas(block_blob_client, 'application', "blobxfer.py", blobxfer_fn, datetime.datetime.utcnow() + datetime.timedelta(hours=1))
        dist_url = commonhelpers.upload_blob_and_create_sas(block_blob_client, 'application', "dist.bat", dist_filename, datetime.datetime.utcnow() + datetime.timedelta(hours=1))


        ####################################################
        # Copy everything on PYTHONPATH to a blob
        ####################################################
        localpythonpath = os.environ.get("PYTHONPATH") #!!should it be able to work without pythonpath being set (e.g. if there was just one file)? Also, is None really the return or is it an exception.
        if localpythonpath == None: raise Exception("Expect local machine to have 'pythonpath' set")
        for i, localpathpart in enumerate(localpythonpath.split(';')):
            blobxfer(r"blobxfer.py --delete --storageaccountkey {} --upload {} {} {}".format(storage_key,storage_account,"pp{}".format(i),"."),
                     wd=localpathpart)
    

        ####################################################
        # Create a job with tasks and run it.
        ####################################################
        job_id = commonhelpers.generate_unique_resource_name(distributable.name)
        credentials = batchauth.SharedKeyCredentials(batch_account, batch_key)
        batch_client = batch.BatchServiceClient(credentials,base_url=batch_service_url)
        job = batchmodels.JobAddParameter(id=job_id, pool_info=batch.models.PoolInformation(pool_id="twoa1"))
        batch_client.job.add(job)

        command_format_string = r"dist.bat {0}"
        task_list = []
        for taskindex in xrange(self.taskcount):
            task = batchmodels.TaskAddParameter(
                id=str(taskindex),
                run_elevated=True,
                resource_files=[batchmodels.ResourceFile(blob_source=distributablep_url, file_path="distributable.p"),
                                batchmodels.ResourceFile(blob_source=blobxfer_url, file_path="blobxfer.py"),
                                batchmodels.ResourceFile(blob_source=dist_url, file_path="dist.bat"),
                                ],
                command_line=command_format_string.format(taskindex),
            )
            task_list.append(task)

        try:
            batch_client.task.add_collection(job_id, task_list)
        except Exception as exception:
            print exception
 
        commonhelpers.wait_for_tasks_to_complete(batch_client, job_id, datetime.timedelta(minutes=25))
 
 
        tasks = batch_client.task.list(job_id) 
        task_ids = [task.id for task in tasks]
 
 
        commonhelpers.print_task_output(batch_client, job_id, task_ids)

def test_fun(runner):
    from fastlmm.util.mapreduce import map_reduce
    def printx(x):
        print x
        return x**2

    result = map_reduce(range(4),
                        mapper=printx,
                        name="printx",
                        runner = runner
                        )
    print result
    print "done"

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.info("Hello")

    if False:
        pass
    elif False:
        from fastlmm.util.runner.blobxfer import main as blobxfermain #https://pypi.io/project/blobxfer/
        batch_service_url, batch_account, batch_key, storage_account, storage_key = [s.strip() for s in open(os.path.expanduser("~")+"/azurebatch/cred.txt").xreadlines()] #!!!cmk make this a param????
        os.chdir(r"c:\deldir\sub")
        c = r"IGNORED --delete --storageaccountkey {} --upload {} pp2 .".format(storage_key, storage_account)
        sys.argv = c.split(" ")
        blobxfermain(exit_is_ok=False)
        print "done"

    elif False: # How to copy a directory to a blob -- only copying new stuff and remove any old stuff
        from fastlmm.util.runner.blobxfer import main as blobxfermain #https://pypi.io/project/blobxfer/
        batch_service_url, batch_account, batch_key, storage_account, storage_key = [s.strip() for s in open(os.path.expanduser("~")+"/azurebatch/cred.txt").xreadlines()] #!!!cmk make this a param????

        localpythonpath = os.environ.get("PYTHONPATH") #!!should it be able to work without pythonpath being set (e.g. if there was just one file)? Also, is None really the return or is it an exception.
        if localpythonpath == None: raise Exception("Expect local machine to have 'pythonpath' set")
        for i, localpathpart in enumerate(localpythonpath.split(';')):
            os.chdir(localpathpart) #!!!cmk at the end put back where it was
            c = r"blobxfer.py --storageaccountkey {} --upload {} {} {}".format(storage_key,storage_account,"test{}".format(i),".")
            sys.argv = c.split(" ")
            blobxfermain(exit_is_ok=False)


        print "done"


    elif True:
        from fastlmm.util.runner.AzureBatch import test_fun
        from fastlmm.util.runner import Local, HPC, LocalMultiProc

        runner = AzureBatch(2)
        #runner = LocalMultiProc(2)
        test_fun(runner)
    elif False:

        #Expect:
        # batch service url, e.g., https://fastlmm.westus.batch.azure.com
        # account, e.g., fastlmm
        # key, e.g. Wsz....

        batch_service_url, batch_account, batch_key, storage_account, storage_key = [s.strip() for s in open(os.path.expanduser("~")+"/azurebatch/cred.txt").xreadlines()]

        #https://azure.microsoft.com/en-us/documentation/articles/batch-python-tutorial/
        # Create the blob client, for use in obtaining references to
        # blob storage containers and uploading files to containers.
        block_blob_client = azureblob.BlockBlobService(account_name=storage_account,account_key=storage_key)

        # Use the blob client to create the containers in Azure Storage if they
        # don't yet exist.
        app_container_name = 'application'
        input_container_name = 'input'
        output_container_name = 'output'
        block_blob_client.create_container(app_container_name, fail_on_exist=False)
        block_blob_client.create_container(input_container_name, fail_on_exist=False)
        block_blob_client.create_container(output_container_name, fail_on_exist=False)

        sas_url = commonhelpers.upload_blob_and_create_sas(block_blob_client, app_container_name, "delme.py", r"C:\Source\carlk\fastlmm\fastlmm\util\runner\delme.py", datetime.datetime.utcnow() + datetime.timedelta(hours=1))  



        job_id = commonhelpers.generate_unique_resource_name("HelloWorld")

    
        credentials = batchauth.SharedKeyCredentials(batch_account, batch_key)


        batch_client = batch.BatchServiceClient(credentials,base_url=batch_service_url)

        job = batchmodels.JobAddParameter(id=job_id, pool_info=batch.models.PoolInformation(pool_id="twoa1"))
        batch_client.job.add(job)

       # see http://azure-sdk-for-python.readthedocs.io/en/latest/ref/azure.batch.html
       # http://azure-sdk-for-python.readthedocs.io/en/latest/ref/azure.batch.models.html?highlight=TaskAddParameter
       #  http://azure-sdk-for-python.readthedocs.io/en/latest/_modules/azure/batch/models/task_add_parameter.html
        task = batchmodels.TaskAddParameter(
            id="HelloWorld",
            run_elevated=True,
            resource_files=[batchmodels.ResourceFile(blob_source=sas_url, file_path="delme.py")],
            command_line=r"c:\Anaconda2\python.exe delme.py",
            #doesn't work command_line=r"python c:\user\tasks\shared\test.py"
            #works command_line=r"cmd /c c:\Anaconda2\python.exe c:\user\tasks\shared\test.py"
            #command_line=r"cmd /c python c:\user\tasks\shared\test.py"
            #command_line=r"cmd /c c:\user\tasks\testbat.bat"
            #command_line=r"cmd /c echo start & c:\Anaconda2\python.exe -c 3/0 & echo Done"
            #command_line=r"c:\Anaconda2\python.exe -c print('ello')"
            #command_line=r"python -c print('hello_from_python')"
            #command_line=commonhelpers.wrap_commands_in_shell('windows', ["python -c print('hello_from_python')"]
        )

        try:
            batch_client.task.add_collection(job_id, [task])
        except Exception as exception:
            print exception
 
        commonhelpers.wait_for_tasks_to_complete(batch_client, job_id, datetime.timedelta(minutes=25))
 
 
        tasks = batch_client.task.list(job_id) 
        task_ids = [task.id for task in tasks]
 
 
        commonhelpers.print_task_output(batch_client, job_id, task_ids)

# get iDistribute working on just two machines with pytnon already installed and no input files, just seralized input and seralized output
# Run python w/o needing to install it on machine
# Copy files to the machines
# Copy python path to the machines
# Understand HDFS and Azure storage
# more than 2 machines (grow)

# DONE # copy python program to machine and run it
# DONE Install Python manully on both machines and then run a python cmd on the machines