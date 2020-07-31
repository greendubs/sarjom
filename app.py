from flask import Flask, render_template, request, flash, url_for, redirect, session
from flask_googlemaps import GoogleMaps, Map
from db.mongo.daos.datastories_dao import DataStoryModel
from forms import PublishForm, LoginForm, DataProcessorForm, MapMetadataForm, MetadataFileForm
import random
from datetime import datetime
import json
from services.file_service import process_upload_file, process_download_file
from services.user_service import process_invite_users
from services.common_service import validate_request
from services.email_service import EmailService
from db.mongo import mongo_connection
from controllers import user_controller, login_controller, project_controller, file_controller, join_controller
from aws_config import config
import requests
import boto3
from botocore.config import Config
import pandas as pd


env = "staging"
mongo_db_connection = mongo_connection.connect_to_db(env)
host = "http://127.0.0.1:5000"

app = Flask(__name__)
app.config.from_object(config[env])
app.config["db_connection"] = mongo_db_connection[config[env].mongo_database]
app.config["db_connection_client"] = mongo_db_connection
app.config["env"] = env
app.config["in_memory_cache"] = {
    "user_id_to_token_id_map": {},
    "token_id_to_secret_key_map": {}
}  # need to replace with redis asap
app.config["master_secret_key"] = config[env].master_secret_key
app.config["login_page_url"] = config[env].login_page_url
app.config["email_sender_address"] = config[env].email_sender_address

GoogleMaps(app)

app.register_blueprint(user_controller.construct_blueprint(app.config), url_prefix="/v2/users")
app.register_blueprint(login_controller.construct_blueprint(app.config), url_prefix="/v2/login")
app.register_blueprint(project_controller.construct_blueprint(app.config), url_prefix="/v2/projects")
app.register_blueprint(file_controller.construct_blueprint(app.config), url_prefix="/v2/files")
app.register_blueprint(join_controller.construct_blueprint(app.config), url_prefix="/v2/joins")

############################################################################################################
#### Other features yet to be integrated. ########################
def define_map(datastory_details):
    print(f'{datastory_details.get("files")} in define map')
    loc_map = Map(identifier='locations-view',
                  lat=datastory_details.get('files')[0].get('location')[1],
                  lng=datastory_details.get('files')[0].get('location')[0],
                  markers=[{'lat': file.get('location')[1],
                            'lng': file.get('location')[0],
                            'infobox': "Image: " + str(file.get('s3_file_path')) +
                                       "\n Location: " + str(tuple(file.get('location'))) +
                                       "\n Date: " + str(file.get('created_at')) +
                                       "\n Contributor: " + str(datastory_details.get('senders')
                                                                .get(str(file.get('sender_id')))
                                                                .get('name'))}
                           for file in datastory_details.get('files')],
                  fit_markers_to_bounds=True)
    return loc_map


def set_project_details_form(form, datastory_details):
    contributors = [details.get('name') for sender, details in datastory_details.get('senders').items()]
    for contributor in contributors:
        form.draft_form.project_details_form.contributors.append_entry(contributor)
    form.draft_form.project_details_form.project_owner.data = datastory_details.get('owner').get('name')
    form.draft_form.project_details_form.organization.data = datastory_details.get('organization')
    if datastory_details.get('content'):
        form.draft_form.editordata.data = datastory_details.get('content')
    else:
        form.draft_form.editordata.data = ""


def generate_random_string():
    # Need to handle unique urls in a better way. We can check if the url already exists in the database and generate
    # new one if exists. But in that case it needs more calls to database. Another option is to use uuid python library
    # but the unique ids are not readable. With current code, we cannot add unique index on the field unique_url. This
    # is because we save drafts in the same collection and drafts zdo not have unique urls. If we have more than one
    # draft, unique index will raise on error.
    all_chars = 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    unique_url = ''.join(random.choice(all_chars) for i in range(3)) + '-' \
                 + ''.join(random.choice(all_chars) for i in range(3)) + '-' \
                 + ''.join(random.choice(all_chars) for i in range(3))
    return unique_url


# Current code makes many calls to database during create data stories and sets the form and map every time. As we use
# Google Maps API setting map every time is expensive and needs to be improved. I was unable to use session to store the
# data. We may need to look into other options to improve performance by reducing the number of calls to database.

# Also, this code does not take into account different users. This need to be updated so that user id is also used when
# making the calls to database. User id must be retrieved from session and must be passed as a variable in all calls.
@app.route('/datastories/', methods=['GET', 'POST'])
def create_datastories():
    form = PublishForm()
    projects = DataStoryModel(mongo_db_connection).get_projects()
    form.draft_form.project_details_form.project_id.choices = [(p.get('_id'), p.get('name')) for p in projects]
    published_stories = DataStoryModel(mongo_db_connection).get_published_datastories()
    for story in published_stories:
        form.draft_form.project_details_form.published_stories.append_entry(story)
    # print(published_stories)
    print('This is get datastories..')
    if request.method == "POST":
        project_id = form.draft_form.project_details_form.project_id.data
        print(project_id)
        datastory_details = DataStoryModel(mongo_db_connection).get_datastory_details(project_id)
        # print(f'{datastory_details} before plot map')

        if not datastory_details.get('files'):
            flash('No files for the project yet. Cannot plot on the map.')
            return render_template('datastory.html', map=None, form=form)

        elif "plot-dataset-on-map" in request.form:
            print('Reached plot on map..')
            loc_map = define_map(datastory_details)
            set_project_details_form(form, datastory_details)
            # print(form.draft_form.project_details_form.contributors)
            # print(form.draft_form.editordata)
            return render_template('datastory.html', map=loc_map, form=form)

        elif "save-draft-datastory" in request.form:
            print('Reached save draft..')
            # print(form.draft_form.editordata.data)
            datastory_details['content'] = form.draft_form.editordata.data
            loc_map = define_map(datastory_details)
            set_project_details_form(form, datastory_details)
            DataStoryModel(mongo_db_connection).save_draft_datastory(datastory_details)  # Add validation
            return render_template('datastory.html', map=loc_map, form=form)

        elif "discard-draft-datastory" in request.form:
            print('Reached discard draft..')
            datastory_details['content'] = ""
            set_project_details_form(form, datastory_details)
            DataStoryModel(mongo_db_connection).save_draft_datastory(datastory_details)
            loc_map = define_map(datastory_details)
            return render_template('datastory.html', map=loc_map, form=form)

        elif "publish-datastory" in request.form:
            print('Reached publish..')
            # print(datastory_details)
            # For now text editor data is stored as html string including images. We may need to extract attachments and
            # store them in another location to save size.
            datastory_details['content'] = form.draft_form.editordata.data
            set_project_details_form(form, datastory_details)
            loc_map = define_map(datastory_details)

            if datastory_details.get('content') == "":
                flash('Please enter your story.')
                return render_template('datastory.html', map=loc_map, form=form)
            else:
                unique_url = generate_random_string()
                datastory_details['unique_url'] = unique_url
                datastory_details['published_date'] = datetime.now()
                form.unique_url.data = unique_url
                DataStoryModel(mongo_db_connection).publish_datastory(datastory_details)
                # Call email service with senders and owner email address
                EmailService().send_email_datastory(datastory_details, env)
                return render_template('datastory.html', map=loc_map, form=form)
    return render_template('datastory.html', map=None, form=form)


@app.route('/datastories/<url>', methods=["GET"])
def view_datastory(url):
    print(url)
    datastory_details = DataStoryModel(mongo_db_connection).view_datastory(url)
    loc_map = define_map(datastory_details)
    return render_template('publish.html', map=loc_map, datastory=datastory_details)


@app.route('/files', methods=["POST"])
def upload_files():
    response = {
        "success_response": None,
        "error_response": None
    }

    request_validity = validate_request(mongo_db_connection, request, "UPLOAD_FILES", "form")

    if (not request_validity["is_valid"]):
        response["error_response"] = {
            "request_validity": request_validity
        }

        return json.dumps(response)

    process_response = process_upload_file(mongo_db_connection, request, request_validity["user"], env)

    if process_response["is_success"]:
        response["success_response"] = process_response
    else:
        response["error_response"] = process_response

    return response


@app.route('/files', methods=["GET"])
def download_files():
    response = {
        "success_response": None,
        "error_response": None
    }

    request_validity = validate_request(mongo_db_connection, request, "DOWNLOAD_FILES", "form")

    if (not request_validity["is_valid"]):
        response["error_response"] = {
            "request_validity": request_validity
        }

        return json.dumps(response)

    process_response = process_download_file(mongo_db_connection, request, request_validity["user"], env)

    if process_response["is_success"]:
        response["success_response"] = process_response
    else:
        response["error_response"] = process_response

    return response


@app.route('/users', methods=["POST"])
def invite_users():
    response = {
        "success_response": None,
        "error_response": None
    }

    request_validity = validate_request(mongo_db_connection, request, "INVITE_USERS", "form")

    if (not request_validity["is_valid"]):
        response["error_response"] = {
            "request_validity": request_validity
        }

        return json.dumps(response)

    process_response = process_invite_users(mongo_db_connection, request, request_validity["user"], env)

    if process_response["is_success"]:
        response["success_response"] = process_response
    else:
        response["error_response"] = process_response

    return response

###################################################################################################################


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


@app.route('/collect', methods=['GET','POST'])
def collect():
    response = requests.post(host + url_for('user_api.check_user_logged_in'),
                             json={'user_id': session.get('user_id')},
                             headers={'token_id': session.get('token_id'),
                                      'access_token': session.get('access_token')})
    if response.json().get('message') == "SUCCESS":
        return render_template('collect.html')
    else:
        return redirect(url_for('login'))


@app.route('/data-licenses', methods=['GET'])
def data_licenses():
    return render_template('data-licenses.html')


@app.route('/projects', methods=["GET"])
def get_create_project():
    response = requests.post(host + url_for('user_api.check_user_logged_in'),
                             json={'user_id': session.get('user_id')},
                             headers={'token_id': session.get('token_id'), 'access_token': session.get('access_token')})
    if response.json().get('message') == "SUCCESS":
        form = DataProcessorForm()
        return render_template('create-project.html', form=form)
    else:
        return redirect(url_for('login'))


@app.route('/projects', methods=["POST"])
def post_create_project():
    form = DataProcessorForm()
    if form.create_project_form.validate_on_submit():
        project_name = form.create_project_form.project_name.data
        project_license = form.create_project_form.license.data
        response = requests.post(host + url_for('project_api.create_project'),
                                 json={'name': project_name, 'license': project_license,
                                       'user_id': session.get('user_id')},
                                 headers={'token_id': session.get('token_id'),
                                          'access_token': session.get('access_token')})
        print(response.json())
        if response.json().get('message') == "SUCCESS":
            session['project_id'] = response.json().get('project').get('_id')
            return render_template('upload-raw-data-files.html', form=form)
        else:
            flash(response.json().get('message'))
    return render_template('create-project.html', form=form)


@app.route('/upload-raw-files', methods=["GET"])
def get_upload_raw_data_files():
    response = requests.post(host + url_for('user_api.check_user_logged_in'),
                             json={'user_id': session.get('user_id')},
                             headers={'token_id': session.get('token_id'),
                                      'access_token': session.get('access_token')})
    if response.json().get('message') == "SUCCESS":
        form = DataProcessorForm()
        return render_template('upload-raw-data-files.html', form=form)
    else:
        return redirect(url_for('login'))


@app.route('/upload-meta-files', methods=["GET"])
def get_upload_metadata_files():
    response = requests.post(host + url_for('user_api.check_user_logged_in'),
                             json={'user_id': session.get('user_id')},
                             headers={'token_id': session.get('token_id'), 'access_token': session.get('access_token')})
    if response.json().get('message') == "SUCCESS":
        form = DataProcessorForm()
        return render_template('upload-metadata-files.html', form=form)
    else:
        return redirect(url_for('login'))


@app.route('/map-meta-files', methods=["GET"])
def get_map_metadata_files():
    response = requests.post(host + url_for('user_api.check_user_logged_in'),
                             json={'user_id': session.get('user_id')},
                             headers={'token_id': session.get('token_id'),
                                      'access_token': session.get('access_token')})
    if response.json().get('message') == "SUCCESS":
        form = DataProcessorForm()
        return render_template('map-metadata-files.html', form=form)
    else:
        return redirect(url_for('login'))


@app.route('/sign_s3')
def sign_s3():
    s3_bucket = config[env].bucket_name
    print(s3_bucket)
    project_id = session.get('project_id')
    file_name = project_id+'/'+request.args.get('file_name')
    print(file_name)
    file_type = request.args.get('file_type')
   # print(project_id)
    s3_client = boto3.client('s3', aws_access_key_id=config[env].access_key_id,
                             aws_secret_access_key=config[env].secret_access_key,
                             config=Config(signature_version='s3v4',region_name='us-west-2'))
    try:
        presigned_post = s3_client.generate_presigned_post(
            s3_bucket,
            file_name,
            Fields={'acl': 'public-read','Content-Type': file_type},
            Conditions=[{'acl': 'public-read'}, {'Content-Type': file_type}],
            ExpiresIn=3000
        )
       # print(presigned_post)
    except Exception as e:
        flash(str(e))

    #print(presigned_post)
    return json.dumps({
        'data':presigned_post,
        'url':f'https://{s3_bucket}.s3.amazonaws.com/{file_name}'
    })


@app.route('/upload-raw-files', methods=["POST"])
def post_upload_raw_data_files():
    form = DataProcessorForm()
    if form.upload_raw_data_form.validate_on_submit():
        file_names = [file.filename for file in form.upload_raw_data_form.raw_data_files.data]
        print(file_names)
        urls = request.form.getlist('s3url-hidden')
        rpaths = request.form.getlist('s3rpath-hidden')
        print(f'urls:{urls}')
        print(f'rpath:{rpaths}')
        project_files = [{"file_name": file_name, "s3_link": url, "relative_s3_path": rpath,"file_type": "RAW"}
                         for (file_name, url, rpath) in zip(file_names,urls,rpaths)]
        print(project_files)
        response = requests.post(host + url_for('file_api.create_file'),
                                 json={'project_id': session.get('project_id'), 'files': project_files,
                                       'user_id': session.get('user_id')},
                                 headers={'token_id': session.get('token_id'),
                                          'access_token': session.get('access_token')})
        print(response.json())
        if response.json().get('message') == "SUCCESS":
            return render_template('upload-metadata-files.html', form=form)
        else:
            flash(response.json().get('message'))
    return render_template('upload-raw-data-files.html', form=form)


@app.route('/upload-meta-files', methods=["POST"])
def post_upload_metadata_files():
    form = DataProcessorForm()
    print('Reached post upload meta data..')
    if form.upload_metadata_form.validate_on_submit():
        file_names = [file.filename for file in form.upload_metadata_form.meta_data_files.data]
        print(file_names)
        urls = request.form.getlist('s3url-hidden')
        rpaths = request.form.getlist('s3rpath-hidden')
        print(f'urls:{urls}')
        print(f'rpath:{rpaths}')
        project_files = [{"file_name": file_name, "s3_link": url, "relative_s3_path": rpath,"file_type": "META_DATA"}
                         for (file_name, url, rpath) in zip(file_names,urls,rpaths)]
        print(project_files)
        response = requests.post(host + url_for('file_api.create_file'),
                                 json={'project_id': session.get('project_id'), 'files': project_files,
                                       'user_id': session.get('user_id')},
                                 headers={'token_id': session.get('token_id'),
                                          'access_token': session.get('access_token')})
        print(response.json())
        if response.json().get('message') == "SUCCESS":
            files = request.files.getlist('upload_metadata_form-meta_data_files')
           # print(files)
            form = MapMetadataForm()
            for i, file in enumerate(files):
                file_read = pd.read_csv(file)
                headers = list(file_read.columns)
                #print(headers)
                #print(file_names)
                subform = MetadataFileForm()
                subform.file_id.data = response.json().get('files')[i].get('_id')
                subform.file_name.data = file_names[i]
                subform.file_columns = headers
                #print(subform.file_name.data, subform.file_columns)
                form.metadata_form_columns.append_entry(subform)
            return render_template('map-metadata-files.html', form=form)
        else:
            flash(response.json().get('message'))
    return render_template('upload-metadata-files.html', form=form)


@app.route('/map-meta-files', methods=["POST"])
def post_map_metadata_files():
    print('Reached map meta data..')
    form = MapMetadataForm()
    print(request.form.getlist('file_name'))
    print(request.form.getlist('file_id'))
    print(request.form.getlist('column_select'))
    print(request.form.getlist('join_select'))
    file_names = set(request.form.getlist('file_name'))
    select_columns = request.form.getlist('column_select')
    join_columns = request.form.getlist('join_select')
    selected_columns = {}
    to_join_columns = {}
    joins = []
    for table in select_columns:
        col, id_html = table.split(';')
        print(col)
        print(id_html)
        id = id_html.split('value="')[1][:-2]
        selected_columns[id] = selected_columns.get(id,[]) + [col]
    print(selected_columns)
    for table in join_columns:
        col, id_html = table.split(';')
        id = id_html.split('value="')[1][:-2]
        to_join_columns[id] = to_join_columns.get(id,[]) + [col]
    print(selected_columns)
    print(to_join_columns)
    for ind, file_id in enumerate(selected_columns):
        joins.append({'file_id_'+str(ind+1):file_id,
                      'columns_for_file_'+str(ind+1):selected_columns.get(file_id),
                      'join_column_for_file_'+str(ind+1):to_join_columns.get(file_id)[0]})
    print(joins)
    joins_flat = [{k: v for d in joins for k, v in d.items()}]
    print(joins_flat)
    response = requests.post(host + url_for('join_api.create_joins'),
                             json={'project_id': session.get('project_id'), 'joins': joins_flat,
                                   'user_id': session.get('user_id')},
                             headers={'token_id': session.get('token_id'),
                                      'access_token': session.get('access_token')})
    print(response.json())
    if response.json().get('message') == "SUCCESS":
        return render_template('upload-success.html', form=form)
    else:
        flash(response.json().get('message'))
    return render_template('map-metadata-files.html', form=form)


@app.route('/login', methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data
        password = form.password.data
        response = requests.post(host + url_for('login_api.login'),
                                 json={'email': email, 'password': password})
        print(f'In login: {response.json()}')
        if response.json().get('message') == "SUCCESS":
            session['user_id'] = response.json().get('user').get('_id')
            session['token_id'] = response.json().get('token_id')
            session['access_token'] = response.json().get('access_token')
            session['logged_in'] = True
            return redirect(url_for('collect'))
    return render_template('login.html', form=form)


@app.route('/logout', methods=["GET", "POST"])
def logout():
    print(f'In logout..')
    session['logged_in'] = False
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(host='0.0.0.0',debug=False)

