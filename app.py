# app.py
import os
import logging
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, flash, send_file, after_this_request
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash # Removed check_password_hash as it's in User model
from pathlib import Path
import uuid
import configparser
import time
import sys
import io
from datetime import datetime

# --- Flask 扩展初始化 ---
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required

# --- Flask 应用实例化 ---
app = Flask(__name__)

# --- 配置加载 ---
BASE_DIR = Path(__file__).parent
config = configparser.ConfigParser()
config_path = BASE_DIR / 'config.ini'

if config_path.exists():
    try:
        config.read(config_path, encoding='utf-8')
        logging.info(f"[Flask App] 成功加载配置: {config_path}")
    except Exception as e:
        logging.error(f"[Flask App] 错误: 加载配置 {config_path} 失败: {e}")
        app.config['SECRET_KEY'] = 'default_secret_key_if_config_fails_CHANGE_ME'
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site_fallback.db'
else:
    logging.warning(f"[Flask App] 警告: 配置未找到: {config_path}, 将使用默认配置。")
    app.config['SECRET_KEY'] = 'default_secret_key_if_config_not_found_CHANGE_ME'
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site_default.db'

app.config['SECRET_KEY'] = config.get('General', 'FLASK_SECRET_KEY', fallback='a_very_fallback_secret_key_CHANGE_ME')
app.config['SQLALCHEMY_DATABASE_URI'] = config.get('General', 'SQLALCHEMY_DATABASE_URI', fallback='sqlite:///site.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['APP_CONFIG'] = config

app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['OUTPUT_FOLDER'] = BASE_DIR / 'output'
app.config['ALLOWED_EXTENSIONS'] = {'pptx'}
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['TEMP_PREVIEW_FOLDER'] = BASE_DIR / 'temp_previews'
app.config['TEMP_PREVIEW_FOLDER'].mkdir(parents=True, exist_ok=True)

FLASK_OUTPUT_BASE_DIR = Path(config.get('General', 'base_output_dir', fallback=str(BASE_DIR / 'output'))).resolve()
FLASK_OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = '请先登录以访问此页面。'
login_manager.login_message_category = 'info'

CELERY_AVAILABLE = False
TTS_MANAGER_AVAILABLE = False
celery_app_instance = None # Initialize celery_app_instance
convert_ppt_to_video_task = None # Initialize task
get_available_tts_voices_web = lambda logger: [] # Default to empty list

try:
    from celery_app import celery_app as celery_instance_from_module # Rename to avoid conflict
    import tasks
    celery_app_instance = celery_instance_from_module
    convert_ppt_to_video_task = tasks.convert_ppt_to_video_task
    get_available_tts_voices_web = tasks.get_available_tts_voices
    CELERY_AVAILABLE = True
except ImportError as e:
    logging.error(f"无法导入 Celery: {e}")
except AttributeError as e_attr: # Catch if celery_app is not in celery_app.py
    logging.error(f"Celery_app.py 中未找到 celery_app 实例: {e_attr}")


try:
    from core_logic.tts_manager_edge import generate_preview_audio, KNOWN_EDGE_VOICES
    TTS_MANAGER_AVAILABLE = True
except ImportError as e:
    logging.error(f"无法导入 TTS 管理器: {e}")

log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
if not app.debug:
    pass
app.logger.setLevel(log_level)

with app.app_context():
    from models import User, TaskRecord
    from forms import RegistrationForm, LoginForm

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

available_voices = []
if CELERY_AVAILABLE and TTS_MANAGER_AVAILABLE:
    try:
        available_voices = get_available_tts_voices_web(app.logger)
        app.logger.info(f"获取到 {len(available_voices)} 个可用语音。")
    except Exception as e:
         app.logger.error(f"获取可用 TTS 语音列表失败: {e}", exc_info=True)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = generate_password_hash(form.password.data)
        app_config = app.config['APP_CONFIG']
        free_limit = app_config.getint('UserRoles', 'free_video_limit', fallback=1)
        user = User(username=form.username.data,
                    email=form.email.data,
                    password_hash=hashed_password,
                    role='free',
                    video_creation_limit=free_limit,
                    videos_created_count=0)
        db.session.add(user)
        db.session.commit()
        flash('恭喜您，注册成功！现在可以登录了。', 'success')
        app.logger.info(f"新用户注册成功: {form.username.data} ({form.email.data})")
        return redirect(url_for('login'))
    return render_template('register.html', title='注册', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            user.last_login = datetime.utcnow()
            db.session.commit()
            app.logger.info(f"用户登录成功: {user.username}")
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else:
            flash('登录失败，请检查邮箱和密码是否正确。', 'danger')
            app.logger.warning(f"用户登录失败: {form.email.data}")
    return render_template('login.html', title='登录', form=form)

@app.route('/logout')
@login_required
def logout():
    app.logger.info(f"用户登出: {current_user.username}")
    logout_user()
    flash('您已成功登出。', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user_tasks = TaskRecord.query.filter_by(user_id=current_user.id).order_by(TaskRecord.created_at.desc()).all()
    return render_template('dashboard.html', title="我的任务", tasks=user_tasks)

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    app_config = app.config['APP_CONFIG']
    if request.method == 'POST':
        if not CELERY_AVAILABLE or not convert_ppt_to_video_task: # Check if task is available
             flash("服务器后台任务处理系统未启动或配置错误，无法处理请求。", "error")
             app.logger.error("Celery or convert_ppt_to_video_task not available for POST request.")
             return render_template('index.html', title="PPT 转视频", voices=available_voices,
                                    can_create=current_user.can_create_video(app_config),
                                    limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 无限制)")

        if not current_user.can_create_video(app_config):
            flash(f"抱歉，{current_user.role} 用户已达到视频创建上限 ({current_user.videos_created_count}/{current_user.video_creation_limit})。", "warning")
            app.logger.warning(f"用户 {current_user.username} 已达视频创建上限。")
            return redirect(url_for('index'))

        if 'pptx_file' not in request.files:
            flash("请选择一个 PPTX 文件上传。", "error")
            return redirect(request.url)
        
        file = request.files['pptx_file']
        if file.filename == '':
            flash("文件名无效，请重新选择文件。", "error")
            return redirect(request.url)

        if file and allowed_file(file.filename):
            original_filename = secure_filename(file.filename)
            safe_stem = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in Path(original_filename).stem)
            timestamp = int(time.time())
            unique_filename_stem = f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_stem}"
            unique_filename = f"{unique_filename_stem}{Path(original_filename).suffix}"
            
            upload_folder_path = Path(app.config['UPLOAD_FOLDER'])
            upload_folder_path.mkdir(parents=True, exist_ok=True)
            filepath = upload_folder_path / unique_filename

            try:
                file.save(filepath)
                app.logger.info(f"用户 {current_user.username} 上传文件已保存: {filepath}")

                voice_id = request.form.get('voice_id')
                if not voice_id and available_voices:
                     voice_id = available_voices[0].get('id', 'zh-CN-XiaoxiaoNeural')
                elif not available_voices and not voice_id:
                    flash("无法获取语音设置或选择，请检查后台服务或选择一个语音。", "error")
                    return render_template('index.html', title="PPT 转视频", voices=available_voices,
                                           can_create=current_user.can_create_video(app_config),
                                           limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 无限制)")

                new_task_record = TaskRecord(
                    user_id=current_user.id,
                    celery_task_id="TEMP_" + uuid.uuid4().hex,
                    original_ppt_filename=original_filename,
                    status='PENDING'
                )
                db.session.add(new_task_record)
                db.session.commit()
                app.logger.info(f"为用户 {current_user.username} 创建了任务记录: ID {new_task_record.id}")

                celery_task = convert_ppt_to_video_task.apply_async(args=[
                    str(filepath.resolve()),
                    str(FLASK_OUTPUT_BASE_DIR.resolve()),
                    voice_id,
                    new_task_record.id,
                    current_user.id
                ])
                
                new_task_record.celery_task_id = celery_task.id
                db.session.commit()
                app.logger.info(f"Celery 任务已发送 (ID: {celery_task.id})，并更新到任务记录 {new_task_record.id}")

                return redirect(url_for('task_status', task_id=celery_task.id))

            except Exception as e:
                app.logger.error(f"文件上传或任务发送失败 for user {current_user.username}: {e}", exc_info=True)
                flash(f"文件上传或处理启动失败: {e}", "error")
                if 'new_task_record' in locals() and hasattr(new_task_record, 'id') and new_task_record.id:
                    try:
                        db.session.delete(new_task_record)
                        db.session.commit()
                        app.logger.info(f"已回滚任务记录 {new_task_record.id} 的创建。")
                    except Exception as e_rollback:
                        app.logger.error(f"回滚任务记录 {new_task_record.id} 失败: {e_rollback}", exc_info=True)

                return render_template('index.html', title="PPT 转视频", voices=available_voices,
                                       can_create=current_user.can_create_video(app_config),
                                       limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 无限制)")
        else:
            flash(f"只允许上传文件类型为: {', '.join(app.config['ALLOWED_EXTENSIONS'])}", "error")
            return redirect(request.url)

    can_create = current_user.can_create_video(app_config)
    limit_info = f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 用户无限制)"
    return render_template('index.html', title="PPT 转视频", voices=available_voices, can_create=can_create, limit_info=limit_info)

@app.route('/preview_tts/<voice_id>', methods=['GET'])
@login_required
def preview_tts(voice_id):
    logger = app.logger
    if not TTS_MANAGER_AVAILABLE:
        return jsonify({"error": "TTS service is unavailable"}), 503
    if not voice_id or voice_id not in KNOWN_EDGE_VOICES:
        return jsonify({"error": "Invalid voice ID"}), 400
    
    lang_prefix = KNOWN_EDGE_VOICES[voice_id].get('lang', 'en').split('-')[0].lower()
    sample_text = "你好，这是一个语音试听示例。" if lang_prefix == 'zh' else "Hello, this is a voice preview."
    temp_audio_file_path_str = None
    try:
        temp_audio_file_path_str = generate_preview_audio(voice_id, logger, text=sample_text)
        if temp_audio_file_path_str:
            temp_audio_file_path = Path(temp_audio_file_path_str)
            @after_this_request
            def cleanup_preview_audio(response):
                try:
                    if temp_audio_file_path.exists(): os.remove(temp_audio_file_path)
                except Exception as e_clean:
                    logger.error(f"删除试听临时文件 {temp_audio_file_path} 失败: {e_clean}", exc_info=True)
                return response
            return send_file(temp_audio_file_path, mimetype='audio/mpeg', as_attachment=False)
        else:
            return jsonify({"error": "Failed to generate preview audio"}), 500
    except Exception as e:
        logger.error(f"生成试听音频时发生意外错误 for voice_id '{voice_id}': {e}", exc_info=True)
        if temp_audio_file_path_str and Path(temp_audio_file_path_str).exists():
            try: os.remove(temp_audio_file_path_str)
            except Exception: pass
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route('/status/<task_id>')
@login_required
def task_status(task_id):
    if not CELERY_AVAILABLE or not celery_app_instance: # Check celery_app_instance
         app.logger.error(f"Attempted to access task status for {task_id} but Celery is not available.")
         flash("后台任务处理系统不可用。", "error")
         return render_template('status.html', title="任务状态", task_id=task_id, initial_status='ERROR', error_message="服务器后台任务处理系统未启动。")

    task_record = TaskRecord.query.filter_by(celery_task_id=task_id, user_id=current_user.id).first()
    if not task_record:
        flash("无权访问此任务状态或任务不存在。", "danger")
        app.logger.warning(f"用户 {current_user.username} 尝试访问不属于他或不存在的任务状态: {task_id}")
        return redirect(url_for('dashboard'))

    task_result_obj = celery_app_instance.AsyncResult(task_id)
    
    # Robustly get task info
    initial_meta = {}
    task_state = task_result_obj.state # Get state first

    try:
        if task_state != 'PENDING' and task_result_obj.info:
            if isinstance(task_result_obj.info, dict):
                initial_meta = task_result_obj.info
            elif task_state == 'FAILURE': # If info is not a dict but state is FAILURE
                initial_meta = {'error': str(task_result_obj.info)} # task.info might be the exception itself
                # Try to get traceback if available
                if hasattr(task_result_obj, 'traceback') and task_result_obj.traceback:
                     initial_meta['traceback'] = str(task_result_obj.traceback)

    except ValueError as e_val: # Handles "Exception information must include..."
        app.logger.error(f"ValueError accessing task.info for {task_id} (State: {task_state}): {e_val}")
        if task_state == 'FAILURE':
            initial_meta = {'error': '任务执行失败，无法获取详细错误信息。请检查 Celery worker 日志。'}
            # Try to get raw backend meta for traceback if possible
            try:
                raw_backend_meta = celery_app_instance.backend.get_task_meta(task_id)
                if isinstance(raw_backend_meta, dict) and raw_backend_meta.get('traceback'):
                    initial_meta['traceback'] = raw_backend_meta.get('traceback')
                elif hasattr(task_result_obj, 'traceback') and task_result_obj.traceback: # Fallback to result object's traceback
                    initial_meta['traceback'] = str(task_result_obj.traceback)
            except Exception as e_raw_meta:
                app.logger.error(f"获取原始后端元数据以获取追踪信息失败 for task {task_id}: {e_raw_meta}")
        else:
            initial_meta = {'error': '无法检索任务信息。'}
    except Exception as e_info:
        app.logger.error(f"访问 task.info 时发生未知错误 for {task_id} (State: {task_state}): {e_info}", exc_info=True)
        initial_meta = {'error': '获取任务信息时发生意外错误。'}

    return render_template('status.html', title="任务状态", task_id=task_id, initial_status=task_state, initial_meta=initial_meta)


@app.route('/tasks/<task_id>/status')
@login_required
def get_task_status(task_id):
    if not CELERY_AVAILABLE or not celery_app_instance:
         return jsonify({'state': 'ERROR', 'error': 'Celery 后台系统未启动'}), 500

    task_record = TaskRecord.query.filter_by(celery_task_id=task_id, user_id=current_user.id).first()
    if not task_record:
        app.logger.warning(f"用户 {current_user.username} 尝试通过 API 访问不属于他或不存在的任务状态: {task_id}")
        return jsonify({'state': 'ERROR', 'error': 'Unauthorized or task not found'}), 403

    task_result_obj = celery_app_instance.AsyncResult(task_id)
    task_state = task_result_obj.state
    
    custom_meta = {}
    try:
        if task_state != 'PENDING' and task_result_obj.info:
            if isinstance(task_result_obj.info, dict):
                custom_meta = task_result_obj.info
            elif task_state == 'FAILURE':
                custom_meta = {'error': str(task_result_obj.info)}
                if hasattr(task_result_obj, 'traceback') and task_result_obj.traceback:
                    custom_meta['traceback'] = str(task_result_obj.traceback)
    except ValueError as e_val:
        app.logger.error(f"API/get_task_status: ValueError accessing task.info for {task_id} (State: {task_state}): {e_val}")
        if task_state == 'FAILURE':
            custom_meta = {'error': '任务失败，详情不可用。'}
            try:
                raw_backend_meta = celery_app_instance.backend.get_task_meta(task_id)
                if isinstance(raw_backend_meta, dict) and raw_backend_meta.get('traceback'):
                    custom_meta['traceback'] = raw_backend_meta.get('traceback')
            except Exception: pass
    except Exception as e_info_api:
        app.logger.error(f"API/get_task_status: Error accessing task.info for {task_id} (State: {task_state}): {e_info_api}", exc_info=True)
        custom_meta = {'error': '无法获取任务元数据。'}


    response = {
        'state': task_state,
        'task_id': task_id, # Use the passed task_id
        'meta': custom_meta
    }

    if task_state == 'SUCCESS' or task_state == 'COMPLETE':
        final_result_filename = task_record.output_video_filename # Get filename from DB record
        response['result'] = final_result_filename 
        try:
             if final_result_filename and isinstance(final_result_filename, str):
                 filename_for_download = Path(final_result_filename).name
                 response['download_url'] = url_for('download_file', filename=filename_for_download, task_record_id=task_record.id)
             else:
                  response['error'] = "任务成功完成，但未生成有效的下载链接。"
                  response['state'] = 'FAILURE' # Override state if no valid download
                  response['meta']['error'] = "任务成功但未返回结果文件路径。"
        except Exception as e:
             response['error'] = f"构造下载链接失败: {e}"
             response['state'] = 'FAILURE'
             response['meta']['error'] = f"构造下载链接失败: {str(e)}"
    return jsonify(response)

@app.route('/output/<filename>')
@login_required
def download_file(filename):
    task_record_id = request.args.get('task_record_id')
    task_record = None
    if task_record_id:
        task_record = TaskRecord.query.filter_by(id=task_record_id, user_id=current_user.id).first()
    
    if not task_record:
        task_record = TaskRecord.query.filter_by(output_video_filename=filename, user_id=current_user.id).first()

    if not task_record:
        flash("无权下载此文件或文件不存在。", "danger")
        app.logger.warning(f"用户 {current_user.username} 尝试下载不属于他或不存在的文件: {filename}")
        return redirect(url_for('dashboard'))

    safe_filename = secure_filename(filename)
    if task_record.output_video_filename != safe_filename:
        flash("文件名不匹配。", "danger")
        app.logger.warning(f"用户 {current_user.username} 下载文件时文件名不匹配: 请求 {safe_filename}, 记录 {task_record.output_video_filename}")
        return redirect(url_for('dashboard'))

    output_base_dir = FLASK_OUTPUT_BASE_DIR
    app.logger.info(f"用户 {current_user.username} 尝试从 '{output_base_dir}' 下载文件 '{safe_filename}' (TaskRecord ID: {task_record.id})。")
    try:
        full_file_path = output_base_dir / safe_filename
        if not full_file_path.exists():
            app.logger.error(f"下载失败：在预期路径 '{full_file_path}' 未找到文件。")
            from werkzeug.exceptions import NotFound
            raise NotFound()
        return send_from_directory(output_base_dir, safe_filename, as_attachment=True)
    except FileNotFoundError:
         from werkzeug.exceptions import NotFound
         raise NotFound()
    except Exception as e:
         app.logger.error(f"处理文件 '{safe_filename}' 下载时发生错误: {e}", exc_info=True)
         flash("文件下载失败。", "error")
         return redirect(url_for('dashboard'))

@app.errorhandler(404)
def page_not_found(e):
    app.logger.warning(f"404 - 页面未找到: {request.url} (错误: {e})")
    return render_template('404.html', title="404 - 页面未找到"), 404

@app.errorhandler(403)
def forbidden(e):
    app.logger.warning(f"403 - 禁止访问: {request.url} (错误: {e})")
    return render_template('403.html', title="403 - 禁止访问"), 403

@app.errorhandler(413)
def file_too_large(e):
    app.logger.warning(f"413 - 上传文件过大: {request.url}")
    flash(f"上传文件过大，最大允许 {app.config['MAX_CONTENT_LENGTH'] / 1024 / 1024:.2f}MB", "error")
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"500 - 服务器内部错误: {request.url} (错误详情: {e})", exc_info=True)
    if db.session.is_active:
        db.session.rollback()
        app.logger.info("数据库会话已回滚由于内部服务器错误。")
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        response = jsonify({'error': 'Internal Server Error'})
        response.status_code = 500
        return response
    return render_template('500.html', title="500 - 服务器错误"), 500

if __name__ == '__main__':
    app.logger.info("在开发模式下运行 Flask 应用...")
    app.run(debug=True, host='0.0.0.0', port=5000)
