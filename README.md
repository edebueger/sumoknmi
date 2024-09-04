## Table of contents
* [General info](#general-info)
* [Components](#components)
* [Setup](#setup)

## General info
AWS lambda function to extract dutch climate data and push the data into sumologic

## Components
This repository consists of following components:

* GetKNMILambda.py
Python3 module, depending on requests module. Can be run as a lambda function, but also from command line.
Run GetKNMILambda.py --help for all command line options.

* cfGetKNMILambda.json
AWS CloudFormation template: installs the lambda function, plus additional resources required for the lambda function to properly execute (role, policies, event trigger, parameters).

* knmilambda.zip
zipped lambda function, for convenience, to be placed on S3 somewhere acessible by cloudformation script

## Setup
Installation instructions:
In your AWS account (if collection function will run as AWS lambda code) : 
* Place lambda zip archive (knmilambda.zip) on one of your accessible S3 buckets, and remember the access URL to this file.
* Run the provided Cloud Formation template in your AWS environment, and provide it with the proper parameters.
* deployment zip file can be created and uploaded as follows: in git directory create subdirectory python. Install requests module in this dir: pip3 install -t python/ requests
Zip this module contents, plus the GetKNMILambda.py file: 
 cd python
 zip -r ../knmilambda.zip .
 cd ..
 zip -g knmilambda.zip GetKNMILambda.py
Upload knmilambda.zip to S3, for example: 
aws s3api put-object --bucket <bucketname> --key knmilambda.zip --body knmilambda.zip
The lambda function can be updated directly by:
aws lambda update-function-code --function-name SumoKNMILambdaFunction --zip-file fileb://knmilambda.zip
