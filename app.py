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
from datetime import datetime

# --- 1. Flask 应用实例化 ---
app = Flask(__name__)

# --- 2. 配置加载 ---
BASE_DIR = Path(__file__).parent
config = configparser.ConfigParser()
config_path = BASE_DIR / 'config.ini'
_initial_logger = logging.getLogger(f"{__name__}_bootstrap")
_initial_logger.addHandler(logging.StreamHandler())
_initial_logger.setLevel(logging.INFO)

if not config_path.exists():
    _initial_logger.warning(f"配置文件未找到: {config_path}, 将使用硬编码的默认值。")
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'hardcoded_secret_key_dev_only_app_v16')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///site_fallback_app_v16.db')
    app.config['CELERY_BROKER_URL'] = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    app.config['CELERY_RESULT_BACKEND'] = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
else:
    try:
        config.read(config_path, encoding='utf-8')
        _initial_logger.info(f"成功从 {config_path} 加载配置。")
        app.config['SECRET_KEY'] = config.get('General', 'FLASK_SECRET_KEY')
        app.config['SQLALCHEMY_DATABASE_URI'] = config.get('General', 'SQLALCHEMY_DATABASE_URI')
        app.config['CELERY_BROKER_URL'] = config.get('Celery', 'broker_url')
        app.config['CELERY_RESULT_BACKEND'] = config.get('Celery', 'result_backend')
    except Exception as e:
        _initial_logger.error(f"加载配置 {config_path} 失败: {e}. 将使用硬编码默认值。")
        app.config['SECRET_KEY'] = 'hardcoded_secret_key_error_fallback_app_v16'
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///site_error_fallback_app_v16.db'
        app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
        app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/0'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['APP_CONFIG'] = config
app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
app.config['OUTPUT_FOLDER'] = BASE_DIR / 'output'
app.config['OUTPUT_FOLDER'].mkdir(parents=True, exist_ok=True)
app.config['ALLOWED_EXTENSIONS'] = {'pptx'}
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['TEMP_PREVIEW_FOLDER'] = BASE_DIR / 'temp_previews'
app.config['TEMP_PREVIEW_FOLDER'].mkdir(parents=True, exist_ok=True)
FLASK_OUTPUT_BASE_DIR = Path(config.get('General', 'base_output_dir', fallback=str(app.config['OUTPUT_FOLDER']))).resolve()
FLASK_OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)

# --- 3. 初始化 Flask 扩展 ---
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, current_user, login_user, logout_user, login_required

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = '请先登录以访问此页面。'
login_manager.login_message_category = 'info'

# --- 4. 模型导入 (在 db 初始化之后) ---
from models import User, TaskRecord

# --- 5. Flask-Login user_loader 回调函数 ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- 6. TTS 模块的导入及配置 ---
TTS_MANAGER_AVAILABLE = False
try:
    from core_logic.tts_manager_edge import generate_preview_audio, KNOWN_EDGE_VOICES
    TTS_MANAGER_AVAILABLE = True
except ImportError as e:
    _initial_logger.error(f"app.py: 无法导入 TTS 管理器: {e}")

# --- 7. 日志配置 ---
log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
app.logger.setLevel(log_level)
app.logger.info(f"Flask 应用 '{app.name}' 已配置。TTS可用: {TTS_MANAGER_AVAILABLE}")

# --- 8. 表单导入 ---
from forms import RegistrationForm, LoginForm

# --- 9. Celery 任务签名导入 (保持延迟) ---
# 这些变量将在首次需要时在路由函数内部导入
convert_ppt_to_video_task_local = None
get_available_tts_voices_web_local = None # 将在需要时从 tasks 导入
CELERY_AVAILABLE = True # 假设 Celery 基础结构可用，具体任务发送时再检查

def get_celery_tasks():
    """辅助函数，用于按需导入 Celery 任务，以避免顶层循环导入。"""
    global convert_ppt_to_video_task_local, get_available_tts_voices_web_local, CELERY_AVAILABLE
    if convert_ppt_to_video_task_local is None: # 只导入一次
        try:
            from tasks import convert_ppt_to_video_task, get_available_tts_voices
            convert_ppt_to_video_task_local = convert_ppt_to_video_task
            get_available_tts_voices_web_local = get_available_tts_voices
            app.logger.info("Celery 任务签名已按需从 tasks.py 加载。")
            CELERY_AVAILABLE = True
        except ImportError as e_tasks_imp:
            app.logger.error(f"app.py: 按需导入任务签名失败: {e_tasks_imp}。Celery 功能将不可用。", exc_info=False)
            CELERY_AVAILABLE = False
        except AttributeError as e_attr_tasks:
            app.logger.error(f"app.py: tasks.py 中未找到任务签名: {e_attr_tasks}。Celery 功能将不可用。", exc_info=False)
            CELERY_AVAILABLE = False
    return CELERY_AVAILABLE

# --- 辅助函数 ---
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

available_voices = [] # 将在首次需要时填充

# --- 路由定义 ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = generate_password_hash(form.password.data)
        app_config_parser = app.config['APP_CONFIG']
        free_limit = app_config_parser.getint('UserRoles', 'free_video_limit', fallback=1)
        user = User(username=form.username.data, email=form.email.data, password_hash=hashed_password, role='free', video_creation_limit=free_limit, videos_created_count=0)
        db.session.add(user); db.session.commit()
        flash('恭喜您，注册成功！现在可以登录了。', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', title='注册', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            user.last_login = datetime.utcnow(); db.session.commit()
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else: flash('登录失败，请检查邮箱和密码是否正确。', 'danger')
    return render_template('login.html', title='登录', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user(); flash('您已成功登出。', 'info'); return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    user_tasks = current_user.tasks.order_by(TaskRecord.created_at.desc()).all()
    return render_template('dashboard.html', title="我的任务", tasks=user_tasks)

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    global available_voices # 允许修改全局的 available_voices
    if not get_celery_tasks(): # 确保任务已加载
        flash("后台任务处理系统当前不可用，请稍后再试。", "error")
        # 即使 Celery 不可用，也尝试加载语音
        if TTS_MANAGER_AVAILABLE and get_available_tts_voices_web_local:
            if not available_voices: # 只加载一次
                 available_voices = get_available_tts_voices_web_local(app.logger)
        return render_template('index.html', title="PPT 转视频", voices=available_voices, can_create=False, limit_info="服务暂时不可用")

    app_config_parser = app.config['APP_CONFIG']
    
    # 填充 available_voices
    if TTS_MANAGER_AVAILABLE and get_available_tts_voices_web_local:
        if not available_voices: # 只加载一次
            available_voices = get_available_tts_voices_web_local(app.logger)

    if request.method == 'POST':
        if not current_user.can_create_video(app_config_parser):
            flash(f"抱歉，{current_user.role} 用户已达到视频创建上限。", "warning")
            return redirect(url_for('index'))
        if 'pptx_file' not in request.files: flash("请选择一个 PPTX 文件上传。", "error"); return redirect(request.url)
        file = request.files['pptx_file']
        if file.filename == '': flash("文件名无效。", "error"); return redirect(request.url)

        if file and allowed_file(file.filename):
            original_filename = secure_filename(file.filename)
            user_upload_dir = Path(app.config['UPLOAD_FOLDER']) / f"user_{current_user.id}" / "original_ppts"
            user_upload_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time())
            unique_ppt_disk_filename = f"{timestamp}_{uuid.uuid4().hex[:8]}_{original_filename}"
            filepath_on_disk = user_upload_dir / unique_ppt_disk_filename
            try:
                file.save(filepath_on_disk)
                voice_id = request.form.get('voice_id') or (available_voices[0].get('id') if available_voices else None)
                if not voice_id: flash("无法获取语音设置。", "error"); return render_template('index.html', title="PPT 转视频", voices=available_voices, can_create=current_user.can_create_video(app_config_parser), limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})")

                new_task_record = TaskRecord(user_id=current_user.id, celery_task_id="TEMP_" + uuid.uuid4().hex, original_ppt_filename=original_filename, original_ppt_path=str(filepath_on_disk.relative_to(BASE_DIR)), status='PENDING')
                db.session.add(new_task_record); db.session.commit()
                
                celery_task_obj = convert_ppt_to_video_task_local.apply_async(args=[
                    str(filepath_on_disk.resolve()), str(FLASK_OUTPUT_BASE_DIR.resolve()),
                    voice_id, new_task_record.id, current_user.id
                ])
                new_task_record.celery_task_id = celery_task_obj.id; db.session.commit()
                app.logger.info(f"Celery 任务已发送 (ID: {celery_task_obj.id}) for user {current_user.username}")
                return redirect(url_for('task_status', task_id=celery_task_obj.id))
            except Exception as e:
                app.logger.error(f"文件上传或任务发送失败: {e}", exc_info=True)
                flash(f"处理失败: {str(e)[:200]}", "error")
                if 'new_task_record' in locals() and hasattr(new_task_record, 'id') and new_task_record.id: # type: ignore
                    try: db.session.delete(new_task_record); db.session.commit() # type: ignore
                    except: pass 
                return render_template('index.html', title="PPT 转视频", voices=available_voices, can_create=current_user.can_create_video(app_config_parser), limit_info=f"({current_user.videos_created_count}/{current_user.video_creation_limit})")
        else:
            flash(f"只允许上传文件类型为: {', '.join(app.config['ALLOWED_EXTENSIONS'])}", "error")
            return redirect(request.url)

    can_create = current_user.can_create_video(app_config_parser)
    limit_info = f"({current_user.videos_created_count}/{current_user.video_creation_limit})" if current_user.role == 'free' else "(VIP 用户无限制)"
    return render_template('index.html', title="PPT 转视频", voices=available_voices, can_create=can_create, limit_info=limit_info)

# ... (其他路由如 preview_tts, status, get_task_status, download_ppt, delete_task, download_file 保持不变) ...
# ... (错误处理路由 @app.errorhandler 保持不变) ...
@app.route('/preview_tts/<voice_id>', methods=['GET'])
@login_required
def preview_tts(voice_id):
    logger = app.logger
    if not TTS_MANAGER_AVAILABLE: return jsonify({"error": "TTS service is unavailable"}), 503
    if not voice_id or voice_id not in KNOWN_EDGE_VOICES: return jsonify({"error": "Invalid voice ID"}), 400
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
                except Exception as e_clean: logger.error(f"删除试听临时文件 {temp_audio_file_path} 失败: {e_clean}", exc_info=True)
                return response
            return send_file(temp_audio_file_path, mimetype='audio/mpeg', as_attachment=False)
        else: return jsonify({"error": "Failed to generate preview audio"}), 500
    except Exception as e:
        logger.error(f"生成试听音频时发生意外错误 for voice_id '{voice_id}': {e}", exc_info=True)
        if temp_audio_file_path_str and Path(temp_audio_file_path_str).exists():
            try: os.remove(temp_audio_file_path_str)
            except Exception: pass
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route('/status/<task_id>')
@login_required
def task_status(task_id):
    celery_app_for_status_route = None
    try:
        from celery_app import celery_app as celery_app_imported_for_status
        celery_app_for_status_route = celery_app_imported_for_status
    except ImportError: app.logger.error("task_status 路由无法导入 celery_app 实例。")
        
    if not CELERY_AVAILABLE or not celery_app_for_status_route:
         flash("后台任务处理系统不可用。", "error")
         return render_template('status.html', title="任务状态", task_id=task_id, initial_status='ERROR', error_message="服务器后台任务处理系统未启动。")
    
    task_record = TaskRecord.query.filter_by(celery_task_id=task_id, user_id=current_user.id).first()
    if not task_record: flash("无权访问此任务状态或任务不存在。", "danger"); return redirect(url_for('dashboard'))
    
    task_result_obj = celery_app_for_status_route.AsyncResult(task_id)
    task_state = task_result_obj.state
    initial_meta = {}
    try:
        if task_state != 'PENDING' and task_result_obj.info:
            if isinstance(task_result_obj.info, dict): initial_meta = task_result_obj.info
            elif task_state == 'FAILURE':
                initial_meta = {'error': str(task_result_obj.info)}
                if hasattr(task_result_obj, 'traceback') and task_result_obj.traceback: initial_meta['traceback'] = str(task_result_obj.traceback)
    except ValueError:
        if task_state == 'FAILURE': initial_meta = {'error': '任务执行失败，无法获取详细错误信息。', 'exc_type': 'UnknownCeleryError'}
        else: initial_meta = {'error': '无法检索任务信息。'}
    except Exception as e_info: initial_meta = {'error': '获取任务信息时发生意外错误。'}
            
    return render_template('status.html', title="任务状态", task_id=task_id, initial_status=task_state, 
                           initial_meta=initial_meta, task_record=task_record)

@app.route('/tasks/<task_id>/status')
@login_required
def get_task_status(task_id):
    celery_app_for_api_route = None
    try:
        from celery_app import celery_app as celery_app_imported_for_api
        celery_app_for_api_route = celery_app_imported_for_api
    except ImportError: app.logger.error("get_task_status API 无法导入 celery_app 实例。")

    if not CELERY_AVAILABLE or not celery_app_for_api_route: return jsonify({'state': 'ERROR', 'error': 'Celery 后台系统未启动'}), 500
    task_record = TaskRecord.query.filter_by(celery_task_id=task_id, user_id=current_user.id).first()
    if not task_record: return jsonify({'state': 'ERROR', 'error': 'Unauthorized or task not found'}), 403
    
    task_result_obj = celery_app_for_api_route.AsyncResult(task_id)
    task_state = task_result_obj.state
    custom_meta = {}
    try:
        if task_state != 'PENDING' and task_result_obj.info:
            if isinstance(task_result_obj.info, dict): custom_meta = task_result_obj.info
            elif task_state == 'FAILURE':
                custom_meta = {'error': str(task_result_obj.info)}
                if hasattr(task_result_obj, 'traceback') and task_result_obj.traceback: custom_meta['traceback'] = str(task_result_obj.traceback)
    except ValueError:
        if task_state == 'FAILURE': custom_meta = {'error': '任务失败，详情不可用。', 'exc_type': 'UnknownCeleryError'}
    except Exception as e_info_api: custom_meta = {'error': '无法获取任务元数据。'}

    response = {'state': task_state, 'task_id': task_id, 'meta': custom_meta}
    if task_state == 'SUCCESS' or task_state == 'COMPLETE':
        final_result_filename = task_record.output_video_filename
        response['result'] = final_result_filename 
        try:
             if final_result_filename and isinstance(final_result_filename, str):
                 response['download_url'] = url_for('download_file', filename=Path(final_result_filename).name, task_record_id=task_record.id)
             else: response['error'] = "任务成功完成，但未生成有效的下载链接。"; response['state'] = 'FAILURE'; response['meta']['error'] = "任务成功但未返回结果文件路径。"
        except Exception as e: response['error'] = f"构造下载链接失败: {e}"; response['state'] = 'FAILURE'; response['meta']['error'] = f"构造下载链接失败: {str(e)}"
    return jsonify(response)

@app.route('/download_ppt/<int:task_record_id>')
@login_required
def download_ppt(task_record_id):
    task_record = TaskRecord.query.filter_by(id=task_record_id, user_id=current_user.id).first_or_404()
    if not task_record.original_ppt_path: flash("原始PPT文件路径未记录或文件不存在。", "warning"); return redirect(url_for('dashboard'))
    ppt_full_path = BASE_DIR / task_record.original_ppt_path
    if not ppt_full_path.is_file(): flash("原始PPT文件未找到。", "error"); return redirect(url_for('dashboard'))
    return send_file(ppt_full_path, as_attachment=True, download_name=task_record.original_ppt_filename)

@app.route('/delete_task/<int:task_record_id>', methods=['POST'])
@login_required
def delete_task(task_record_id):
    celery_app_for_delete_route = None
    try:
        from celery_app import celery_app as celery_app_imported_for_delete
        celery_app_for_delete_route = celery_app_imported_for_delete
    except ImportError: app.logger.error("delete_task 路由无法导入 celery_app 实例。")

    task_to_delete = TaskRecord.query.filter_by(id=task_record_id, user_id=current_user.id).first()
    if not task_to_delete: flash("无法找到要删除的任务或无权操作。", "danger"); return redirect(url_for('dashboard'))
    
    celery_task_id_to_revoke = task_to_delete.celery_task_id
    output_video_filename_to_delete = task_to_delete.output_video_filename
    original_ppt_path_to_delete = task_to_delete.original_ppt_path
    
    try:
        if CELERY_AVAILABLE and celery_app_for_delete_route and celery_task_id_to_revoke and not celery_task_id_to_revoke.startswith("TEMP_"):
            celery_app_for_delete_route.control.revoke(celery_task_id_to_revoke, terminate=True, signal='SIGKILL')
        
        db.session.delete(task_to_delete); db.session.commit()
        flash(f"任务 (ID: {task_record_id}) 已成功从记录中删除。", "success")
        
        if output_video_filename_to_delete:
            video_file_path = FLASK_OUTPUT_BASE_DIR / output_video_filename_to_delete
            if video_file_path.exists():
                try: os.remove(video_file_path)
                except Exception as e_vf_del: app.logger.error(f"删除视频文件失败: {e_vf_del}")
        
        if original_ppt_path_to_delete:
            ppt_file_path = BASE_DIR / original_ppt_path_to_delete
            if ppt_file_path.exists():
                try: 
                    os.remove(ppt_file_path)
                    try: ppt_file_path.parent.rmdir() 
                    except OSError: pass 
                except Exception as e_ppt_del: app.logger.error(f"删除原始PPT文件失败: {e_ppt_del}")
    except Exception as e:
        db.session.rollback(); flash(f"删除任务时发生错误: {e}", "danger")
    return redirect(url_for('dashboard'))

@app.route('/output/<filename>')
@login_required
def download_file(filename):
    task_record_id = request.args.get('task_record_id')
    task_record = None
    if task_record_id:
        try: task_record_id = int(task_record_id); task_record = TaskRecord.query.filter_by(id=task_record_id, user_id=current_user.id).first()
        except ValueError: flash("无效的任务记录ID格式。", "danger"); return redirect(url_for('dashboard'))
    if not task_record: task_record = TaskRecord.query.filter_by(output_video_filename=filename, user_id=current_user.id).first()
    if not task_record: flash("无权下载此文件或文件不存在。", "danger"); return redirect(url_for('dashboard'))
    safe_filename = secure_filename(filename)
    if task_record.output_video_filename != safe_filename: flash("文件名不匹配。", "danger"); return redirect(url_for('dashboard'))
    output_base_dir = FLASK_OUTPUT_BASE_DIR
    try:
        full_file_path = output_base_dir / safe_filename
        if not full_file_path.exists(): from werkzeug.exceptions import NotFound; raise NotFound()
        return send_from_directory(output_base_dir, safe_filename, as_attachment=True)
    except FileNotFoundError: from werkzeug.exceptions import NotFound; raise NotFound()
    except Exception as e: flash("文件下载失败。", "error"); return redirect(url_for('dashboard'))

@app.errorhandler(404)
def page_not_found(e): return render_template('404.html', title="404 - 页面未找到"), 404
@app.errorhandler(403)
def forbidden(e): return render_template('403.html', title="403 - 禁止访问"), 403
@app.errorhandler(413)
def file_too_large(e): flash(f"上传文件过大，最大允许 {app.config['MAX_CONTENT_LENGTH'] / 1024 / 1024:.2f}MB", "error"); return redirect(url_for('index'))
@app.errorhandler(500)
def internal_server_error(e):
    app.logger.error(f"500 - 服务器内部错误: {request.url} (错误详情: {e})", exc_info=True)
    if db.session.is_active: db.session.rollback(); app.logger.info("数据库会话已回滚。")
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify({'error': 'Internal Server Error'}), 500
    return render_template('500.html', title="500 - 服务器错误"), 500


if __name__ == '__main__':
    app.logger.info(f"启动 Flask 应用 '{app.name}'...")
    app.run(debug=True, host='0.0.0.0', port=5000)
