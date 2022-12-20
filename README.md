# lambda
Lambda Extensions for CloudKommand. Deploy AWS Lambda Resources


## Layer Extension

### Python
- If you only want popular libraries, either specify them as strings inside "requirements" in kommand.json or include a requirements.txt file at the base of the component folder (and set the flag "requirements.txt" to true in kommand.json). 
- If you are deploying a layer with custom code, all files should be placed inside a folder at the root of the component folder. We recommend for almost all use cases naming the folder "python", as the files are able to be imported by AWS Lambda functions without adding anything to the path.
