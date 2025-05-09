# app.py
import os
import logging
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, flash, send_file, after_this_request
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
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
from flask_login import LoginManager, current_user, login_user, logout_user, login_required

# --- Flask 应用实例化 ---
app = Flask(__name__) # Flask app 实例必须首先创建

# --- 配置加载 ---
BASE_DIR = Path(__file__).parent
config = configparser.ConfigParser() # 这是 configparser 对象
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

# 从解析的 config 对象设置 Flask 配置
app.config['SECRET_KEY'] = config.get('General', 'FLASK_SECRET_KEY', fallback='a_very_fallback_secret_key_CHANGE_ME')
app.config['SQLALCHEMY_DATABASE_URI'] = config.get('General', 'SQLALCHEMY_DATABASE_URI', fallback='sqlite:///site.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['APP_CONFIG'] = config # 将 configparser 对象存入 Flask app 的配置中

# Celery 配置也从 config.ini 读取，并传递给 create_celery_app
app.config['CELERY_BROKER_URL'] = config.get('Celery', 'broker_url', fallback='redis://localhost:6379/0')
app.config['CELERY_RESULT_BACKEND'] = config.get('Celery', 'result_backend', fallback='redis://localhost:6379/0')


app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True) # 确保上传目录存在
app.config['OUTPUT_FOLDER'] = BASE_DIR / 'output'
app.config['OUTPUT_FOLDER'].mkdir(parents=True, exist_ok=True) # 确保输出目录存在
app.config['ALLOWED_EXTENSIONS'] = {'pptx'}
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['TEMP_PREVIEW_FOLDER'] = BASE_DIR / 'temp_previews'
app.config['TEMP_PREVIEW_FOLDER'].mkdir(parents=True, exist_ok=True)

FLASK_OUTPUT_BASE_DIR = Path(config.get('General', 'base_output_dir', fallback=str(app.config['OUTPUT_FOLDER']))).resolve()
FLASK_OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = '请先登录以访问此页面。'
login_manager.login_message_category = 'info'

# --- Celery 和 TTS 模块的导入 ---
# Celery 实例现在通过 celery_utils.py 创建和获取
celery_app_instance = None
convert_ppt_to_video_task = None
get_available_tts_voices_web = lambda logger_param: []

CELERY_AVAILABLE = False
TTS_MANAGER_AVAILABLE = False

try:
    from celery_utils import create_celery_app # 从 celery_utils.py 导入工厂函数
    celery_app_instance = create_celery_app(app) # 使用当前 Flask app 创建 Celery 实例
    CELERY_AVAILABLE = True
    logging.info("Celery app instance created and configured via celery_utils.")
except ImportError as e:
    logging.error(f"无法导入或创建 Celery app 实例 (celery_utils.py): {e}", exc_info=True)
except Exception as e_celery_create:
    logging.error(f"创建 Celery app 实例时出错: {e_celery_create}", exc_info=True)

try:
    from core_logic.tts_manager_edge import generate_preview_audio, KNOWN_EDGE_VOICES
    TTS_MANAGER_AVAILABLE = True
except ImportError as e:
    logging.error(f"无法导入 TTS 管理器: {e}")

# --- 日志配置 ---
log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
app.logger.setLevel(log_level) # 设置 Flask logger 级别

# --- 模型和表单的导入 ---
with app.app_context():
    from models import User, TaskRecord
    from forms import RegistrationForm, LoginForm

# --- 导入 Celery tasks ---
if CELERY_AVAILABLE and celery_app_instance is not None:
    try:
        import tasks # tasks.py 会 'from celery_utils import celery_app_instance as celery'
        convert_ppt_to_video_task = tasks.convert_ppt_to_video_task
        get_available_tts_voices_web = tasks.get_available_tts_voices
        if not convert_ppt_to_video_task:
            app.logger.error("tasks.convert_ppt_to_video_task 未能成功加载！")
            CELERY_AVAILABLE = False
        else:
            app.logger.info("Celery tasks (convert_ppt_to_video_task) 成功加载。")
    except ImportError as e:
        app.logger.error(f"无法从 tasks.py 导入任务: {e}", exc_info=True)
        CELERY_AVAILABLE = False
    except AttributeError as e_attr:
        app.logger.error(f"tasks.py 中未找到 'convert_ppt_to_video_task' 或 'get_available_tts_voices': {e_attr}", exc_info=True)
        CELERY_AVAILABLE = False
else:
    app.logger.warning("Celery 不可用或 celery_app_instance 未初始化，无法加载 Celery 任务。")


# --- 辅助函数 ---
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

available_voices = []
if CELERY_AVAILABLE and TTS_MANAGER_AVAILABLE and get_available_tts_voices_web:
    try:
        available_voices = get_available_tts_voices_web(app.logger)
        app.logger.info(f"获取到 {len(available_voices)} 个可用语音。")
    except Exception as e:
         app.logger.error(f"获取可用 TTS 语音列表失败: {e}", exc_info=True)


# --- 认证路由 ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = generate_password_hash(form.password.data)
        app_config_parser = app.config['APP_CONFIG']
        free_limit = app_config_parser.getint('UserRoles', 'free_video_limit', fallback=1)
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

# --- 应用核心路由 ---
@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    app_config_parser = app.config['APP_CONFIG']
    if request.method == 'POST':
        if not CELERY_AVAILABLE or not convert_ppt_to_video_task:
             flash("服务器后台任务处理系统未启动或配置错误，无法处理请求。", "error")
             app.logger.error("Celery 或 convert_ppt_to_video_task 在 POST 请求时不可用。")
             return render_template('index.html', title="PPT 转视频", voices=available_voices,
                                    can_create=current_user.can_create_video(app_config_parser),
                                    limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 无限制)")

        if not current_user.can_create_video(app_config_parser):
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
            
            # 将原始PPT文件保存到用户专属的子目录或用唯一ID命名，以便后续下载
            # 例如： user_<user_id>/original_ppts/timestamp_filename.pptx
            user_upload_dir = Path(app.config['UPLOAD_FOLDER']) / f"user_{current_user.id}" / "original_ppts"
            user_upload_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = int(time.time())
            unique_ppt_filename = f"{timestamp}_{uuid.uuid4().hex[:8]}_{original_filename}"
            filepath = user_upload_dir / unique_ppt_filename # 存储原始PPT的路径

            try:
                file.save(filepath)
                app.logger.info(f"用户 {current_user.username} 上传文件已保存到: {filepath}")

                voice_id = request.form.get('voice_id')
                if not voice_id and available_voices:
                     voice_id = available_voices[0].get('id', 'zh-CN-XiaoxiaoNeural')
                elif not available_voices and not voice_id:
                    flash("无法获取语音设置或选择，请检查后台服务或选择一个语音。", "error")
                    return render_template('index.html', title="PPT 转视频", voices=available_voices,
                                           can_create=current_user.can_create_video(app_config_parser),
                                           limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 无限制)")

                new_task_record = TaskRecord(
                    user_id=current_user.id,
                    celery_task_id="TEMP_" + uuid.uuid4().hex,
                    original_ppt_filename=original_filename, # 存储原始文件名
                    original_ppt_path=str(filepath.relative_to(BASE_DIR)), # 存储相对路径以便后续下载
                    status='PENDING'
                )
                db.session.add(new_task_record)
                db.session.commit()
                app.logger.info(f"为用户 {current_user.username} 创建了任务记录: ID {new_task_record.id}, PPT路径: {new_task_record.original_ppt_path}")

                if not convert_ppt_to_video_task:
                    flash("任务处理功能当前不可用，请稍后再试。", "error")
                    app.logger.error("convert_ppt_to_video_task 未定义，无法发送 Celery 任务。")
                    db.session.delete(new_task_record)
                    db.session.commit()
                    return redirect(url_for('index'))

                celery_task_obj = convert_ppt_to_video_task.apply_async(args=[
                    str(filepath.resolve()), # 传递给任务的仍是绝对路径
                    str(FLASK_OUTPUT_BASE_DIR.resolve()),
                    voice_id,
                    new_task_record.id,
                    current_user.id
                ])
                
                new_task_record.celery_task_id = celery_task_obj.id
                db.session.commit()
                app.logger.info(f"Celery 任务已发送 (ID: {celery_task_obj.id})，并更新到任务记录 {new_task_record.id}")

                return redirect(url_for('task_status', task_id=celery_task_obj.id))

            except Exception as e:
                app.logger.error(f"文件上传或任务发送失败 for user {current_user.username}: {e}", exc_info=True)
                flash(f"文件上传或处理启动失败: {str(e)[:200]}", "error")
                if 'new_task_record' in locals() and hasattr(new_task_record, 'id') and new_task_record.id: # type: ignore
                    try:
                        db.session.delete(new_task_record) # type: ignore
                        db.session.commit()
                        app.logger.info(f"已回滚任务记录 {new_task_record.id} 的创建。") # type: ignore
                    except Exception as e_rollback:
                        app.logger.error(f"回滚任务记录 {new_task_record.id} 失败: {e_rollback}", exc_info=True) # type: ignore
                return render_template('index.html', title="PPT 转视频", voices=available_voices,
                                       can_create=current_user.can_create_video(app_config_parser),
                                       limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 无限制)")
        else:
            flash(f"只允许上传文件类型为: {', '.join(app.config['ALLOWED_EXTENSIONS'])}", "error")
            return redirect(request.url)

    can_create = current_user.can_create_video(app_config_parser)
    limit_info = f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 用户无限制)"
    return render_template('index.html', title="PPT 转视频", voices=available_voices, can_create=can_create, limit_info=limit_info)


@app.route('/preview_tts/<voice_id>', methods=['GET'])
@login_required
def preview_tts(voice_id):
    # ... (此部分代码与之前版本相同，保持不变) ...
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
    if not CELERY_AVAILABLE or not celery_app_instance:
         app.logger.error(f"尝试访问任务状态 {task_id} 但 Celery 不可用。")
         flash("后台任务处理系统不可用。", "error")
         return render_template('status.html', title="任务状态", task_id=task_id, initial_status='ERROR', error_message="服务器后台任务处理系统未启动。")

    task_record = TaskRecord.query.filter_by(celery_task_id=task_id, user_id=current_user.id).first()
    if not task_record:
        flash("无权访问此任务状态或任务不存在。", "danger")
        app.logger.warning(f"用户 {current_user.username} 尝试访问不属于他或不存在的任务状态: {task_id}")
        return redirect(url_for('dashboard'))

    task_result_obj = celery_app_instance.AsyncResult(task_id)
    task_state = task_result_obj.state
    initial_meta = {}

    try:
        if task_state != 'PENDING' and task_result_obj.info:
            if isinstance(task_result_obj.info, dict):
                initial_meta = task_result_obj.info
            elif task_state == 'FAILURE':
                initial_meta = {'error': str(task_result_obj.info)}
                if hasattr(task_result_obj, 'traceback') and task_result_obj.traceback:
                     initial_meta['traceback'] = str(task_result_obj.traceback)
    except ValueError:
        app.logger.warning(f"获取任务 {task_id} (状态: {task_state}) 的元信息时出现 ValueError。")
        if task_state == 'FAILURE':
            initial_meta = {'error': '任务执行失败，无法获取详细错误信息。请检查 Celery worker 日志。', 'exc_type': 'UnknownCeleryError'}
            try:
                raw_backend_meta = celery_app_instance.backend.get_task_meta(task_id)
                if isinstance(raw_backend_meta, dict) and raw_backend_meta.get('traceback'):
                    initial_meta['traceback'] = raw_backend_meta.get('traceback')
            except Exception: pass
        else:
            initial_meta = {'error': '无法检索任务信息。'}
    except Exception as e_info:
        app.logger.error(f"访问 task.info 时发生未知错误 for {task_id} (State: {task_state}): {e_info}", exc_info=True)
        initial_meta = {'error': '获取任务信息时发生意外错误。'}
        
    return render_template('status.html', title="任务状态", task_id=task_id, initial_status=task_state, 
                           initial_meta=initial_meta, task_record=task_record) # 传递 task_record

@app.route('/tasks/<task_id>/status')
@login_required
def get_task_status(task_id):
    # ... (此部分代码与之前版本类似，主要确保错误处理健壮性) ...
    if not CELERY_AVAILABLE or not celery_app_instance:
         return jsonify({'state': 'ERROR', 'error': 'Celery 后台系统未启动'}), 500

    task_record = TaskRecord.query.filter_by(celery_task_id=task_id, user_id=current_user.id).first()
    if not task_record:
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
    except ValueError:
        app.logger.warning(f"API/get_task_status: 获取任务 {task_id} (状态: {task_state}) 的元信息时出现 ValueError。")
        if task_state == 'FAILURE':
            custom_meta = {'error': '任务失败，详情不可用。', 'exc_type': 'UnknownCeleryError'}
            try:
                raw_backend_meta = celery_app_instance.backend.get_task_meta(task_id)
                if isinstance(raw_backend_meta, dict) and raw_backend_meta.get('traceback'):
                    custom_meta['traceback'] = raw_backend_meta.get('traceback')
            except Exception: pass
    except Exception as e_info_api:
        app.logger.error(f"API/get_task_status: Error accessing task.info for {task_id} (State: {task_state}): {e_info_api}", exc_info=True)
        custom_meta = {'error': '无法获取任务元数据。'}

    response = {'state': task_state, 'task_id': task_id, 'meta': custom_meta}

    if task_state == 'SUCCESS' or task_state == 'COMPLETE':
        final_result_filename = task_record.output_video_filename
        response['result'] = final_result_filename 
        try:
             if final_result_filename and isinstance(final_result_filename, str):
                 filename_for_download = Path(final_result_filename).name
                 response['download_url'] = url_for('download_file', filename=filename_for_download, task_record_id=task_record.id)
             else:
                  response['error'] = "任务成功完成，但未生成有效的下载链接。"
                  response['state'] = 'FAILURE'
                  response['meta']['error'] = "任务成功但未返回结果文件路径。"
        except Exception as e:
             response['error'] = f"构造下载链接失败: {e}"
             response['state'] = 'FAILURE'
             response['meta']['error'] = f"构造下载链接失败: {str(e)}"
    return jsonify(response)


# --- 新增：下载原始PPT文件的路由 ---
@app.route('/download_ppt/<int:task_record_id>')
@login_required
def download_ppt(task_record_id):
    task_record = TaskRecord.query.filter_by(id=task_record_id, user_id=current_user.id).first_or_404()
    
    if not task_record.original_ppt_path:
        flash("原始PPT文件路径未记录或文件不存在。", "warning")
        app.logger.warning(f"用户 {current_user.username} 尝试下载任务 {task_record_id} 的PPT，但路径未记录。")
        return redirect(url_for('dashboard'))

    # original_ppt_path 存储的是相对于 BASE_DIR 的路径
    ppt_full_path = BASE_DIR / task_record.original_ppt_path
    
    if not ppt_full_path.is_file():
        flash("原始PPT文件未找到。", "error")
        app.logger.error(f"用户 {current_user.username} 尝试下载任务 {task_record_id} 的PPT，但文件 {ppt_full_path} 未找到。")
        return redirect(url_for('dashboard'))
        
    app.logger.info(f"用户 {current_user.username} 正在下载任务 {task_record_id} 的原始PPT: {task_record.original_ppt_filename}")
    return send_file(ppt_full_path, as_attachment=True, download_name=task_record.original_ppt_filename)


# --- 新增：删除任务的路由 ---
@app.route('/delete_task/<int:task_record_id>', methods=['POST']) # 只允许 POST 请求删除
@login_required
def delete_task(task_record_id):
    task_to_delete = TaskRecord.query.filter_by(id=task_record_id, user_id=current_user.id).first()
    if not task_to_delete:
        flash("无法找到要删除的任务或无权操作。", "danger")
        return redirect(url_for('dashboard'))

    celery_task_id_to_revoke = task_to_delete.celery_task_id
    output_video_filename_to_delete = task_to_delete.output_video_filename
    original_ppt_path_to_delete = task_to_delete.original_ppt_path

    try:
        # 1. 尝试从 Celery 撤销任务 (如果还在运行或排队)
        if CELERY_AVAILABLE and celery_app_instance and celery_task_id_to_revoke and not celery_task_id_to_revoke.startswith("TEMP_"):
            # TERMINATE 信号会尝试杀死正在运行的任务，REVOKE 只对排队中的任务有效
            celery_app_instance.control.revoke(celery_task_id_to_revoke, terminate=True, signal='SIGKILL')
            app.logger.info(f"用户 {current_user.username} 请求撤销 Celery 任务: {celery_task_id_to_revoke}")

        # 2. 删除数据库记录
        db.session.delete(task_to_delete)
        db.session.commit()
        flash(f"任务 (ID: {task_record_id}) 已成功从记录中删除。", "success")
        app.logger.info(f"用户 {current_user.username} 已删除任务记录: {task_record_id}")

        # 3. (可选但推荐) 删除服务器上的相关文件
        if output_video_filename_to_delete:
            video_file_path = FLASK_OUTPUT_BASE_DIR / output_video_filename_to_delete
            if video_file_path.exists():
                try:
                    os.remove(video_file_path)
                    app.logger.info(f"已删除视频文件: {video_file_path}")
                except Exception as e_vf_del:
                    app.logger.error(f"删除视频文件 {video_file_path} 失败: {e_vf_del}")
        
        if original_ppt_path_to_delete:
            ppt_file_path = BASE_DIR / original_ppt_path_to_delete # original_ppt_path 是相对路径
            if ppt_file_path.exists():
                try:
                    os.remove(ppt_file_path)
                    app.logger.info(f"已删除原始PPT文件: {ppt_file_path}")
                    # 考虑是否删除 user_<id>/original_ppts 这个目录如果它变空了
                except Exception as e_ppt_del:
                    app.logger.error(f"删除原始PPT文件 {ppt_file_path} 失败: {e_ppt_del}")

    except Exception as e:
        db.session.rollback()
        flash(f"删除任务时发生错误: {e}", "danger")
        app.logger.error(f"用户 {current_user.username} 删除任务 {task_record_id} 时出错: {e}", exc_info=True)
        
    return redirect(url_for('dashboard'))


@app.route('/output/<filename>')
@login_required
def download_file(filename):
    # ... (此部分代码与之前版本类似，主要确保权限和文件存在性检查) ...
    task_record_id = request.args.get('task_record_id')
    task_record = None
    if task_record_id:
        try:
            task_record_id = int(task_record_id)
            task_record = TaskRecord.query.filter_by(id=task_record_id, user_id=current_user.id).first()
        except ValueError:
            flash("无效的任务记录ID格式。", "danger")
            return redirect(url_for('dashboard'))
            
    if not task_record:
        task_record = TaskRecord.query.filter_by(output_video_filename=filename, user_id=current_user.id).first()

    if not task_record:
        flash("无权下载此文件或文件不存在。", "danger")
        app.logger.warning(f"用户 {current_user.username} 尝试下载不属于他或不存在的文件: {filename} (task_record_id: {request.args.get('task_record_id')})")
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

# --- 错误处理路由 ---
@app.errorhandler(404)
def page_not_found(e):
    # ... (保持不变) ...
    app.logger.warning(f"404 - 页面未找到: {request.url} (错误: {e})")
    return render_template('404.html', title="404 - 页面未找到"), 404

@app.errorhandler(403)
def forbidden(e):
    # ... (保持不变) ...
    app.logger.warning(f"403 - 禁止访问: {request.url} (错误: {e})")
    return render_template('403.html', title="403 - 禁止访问"), 403

@app.errorhandler(413)
def file_too_large(e):
    # ... (保持不变) ...
    app.logger.warning(f"413 - 上传文件过大: {request.url}")
    flash(f"上传文件过大，最大允许 {app.config['MAX_CONTENT_LENGTH'] / 1024 / 1024:.2f}MB", "error")
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_server_error(e):
    # ... (保持不变，确保回滚逻辑) ...
    app.logger.error(f"500 - 服务器内部错误: {request.url} (错误详情: {e})", exc_info=True)
    original_exception = getattr(e, "original_exception", None)
    if isinstance(original_exception, (ConnectionRefusedError, TimeoutError)) or "OperationalError" in str(original_exception): # type: ignore
        if db.session.is_active:
            db.session.rollback()
            app.logger.info("数据库会话由于连接或操作错误已回滚。")
    elif db.session.is_active:
        db.session.rollback()
        app.logger.info("数据库会话由于内部服务器错误已回滚。")

    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        response = jsonify({'error': 'Internal Server Error'})
        response.status_code = 500
        return response
    return render_template('500.html', title="500 - 服务器错误"), 500

if __name__ == '__main__':
    app.logger.info("在开发模式下运行 Flask 应用...")
    app.run(debug=True, host='0.0.0.0', port=5000)
