# app.py
import os
import logging
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, flash, send_file, after_this_request # 导入 flash, send_file, after_this_request
from werkzeug.utils import secure_filename
from pathlib import Path
import uuid
import configparser
import time
import sys
import io # 用于 BytesIO

# 从 celery_app 导入 Celery 应用实例和任务
try:
    import celery_app
    import tasks

    celery_app_instance = celery_app.celery_app
    convert_ppt_to_video_task = tasks.convert_ppt_to_video_task
    get_available_tts_voices_web = tasks.get_available_tts_voices # 获取用于 Web 显示的语音列表函数

    CELERY_AVAILABLE = True
except ImportError as e:
    logging.error(f"FATAL ERROR: 无法导入 Celery 应用或任务模块: {e}")
    print(f"FATAL ERROR: 无法导入 Celery 应用或任务模块: {e}")
    print("请确保 celery_app.py 和 tasks.py 文件存在且在 Python 搜索路径中，并且安装了所有依赖。")
    CELERY_AVAILABLE = False
except Exception as e:
    logging.error(f"FATAL ERROR: 导入 Celery 应用或任务时发生意外错误: {e}", exc_info=True)
    print(f"FATAL ERROR: 导入 Celery 应用或任务时发生意外错误: {e}")
    print("请检查 tasks.py 或其依赖模块中的错误。")
    CELERY_AVAILABLE = False

# --- 导入核心逻辑中的 TTS 管理器 ---
try:
    from core_logic.tts_manager_edge import generate_preview_audio, KNOWN_EDGE_VOICES # 直接导入试听函数和已知语音
    TTS_MANAGER_AVAILABLE = True
except ImportError as e:
    logging.error(f"FATAL ERROR: 无法导入 TTS 管理器: {e}")
    TTS_MANAGER_AVAILABLE = False


# --- Flask 应用实例化 ---
app = Flask(__name__)

# --- Flask 应用配置 ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback_secret_key_for_dev_or_testing')
BASE_DIR = Path(__file__).parent
app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['OUTPUT_FOLDER'] = BASE_DIR / 'output'
app.config['ALLOWED_EXTENSIONS'] = {'pptx'}
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
# 为试听功能创建一个临时文件目录 (Flask 可以安全访问的)
app.config['TEMP_PREVIEW_FOLDER'] = BASE_DIR / 'temp_previews'
app.config['TEMP_PREVIEW_FOLDER'].mkdir(parents=True, exist_ok=True) # 确保目录存在


# --- 配置解析器 ---
config = configparser.ConfigParser()
config_path = BASE_DIR / 'config.ini'
if config_path.exists():
    try:
        config.read(config_path, encoding='utf-8')
        logging.info(f"[Flask App] 成功加载配置: {config_path}")
    except Exception as e:
        logging.error(f"[Flask App] 错误: 加载配置 {config_path} 失败: {e}")
else:
    logging.warning(f"[Flask App] 警告: 配置未找到: {config_path}")

FLASK_OUTPUT_BASE_DIR = Path(config.get('General', 'base_output_dir', fallback=str(BASE_DIR / 'output'))).resolve()
logging.info(f"Flask 应用将从基础目录 '{FLASK_OUTPUT_BASE_DIR}' 提供文件下载。")
try:
    FLASK_OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)
    logging.info(f"确保 Flask 下载基础目录存在: {FLASK_OUTPUT_BASE_DIR}")
except OSError as e:
    logging.error(f"Web 应用无法创建或访问 Flask 下载基础目录 {FLASK_OUTPUT_BASE_DIR}: {e}")


# --- 辅助函数 ---
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_config():
    return config

def get_logger_for_web():
    return app.logger # 使用 Flask 的 logger


available_voices = []
if CELERY_AVAILABLE and TTS_MANAGER_AVAILABLE: # 也检查 TTS 管理器是否可用
    try:
        # 注意: get_available_tts_voices_web 来自 tasks.py, 它内部调用 core_logic
        available_voices = get_available_tts_voices_web(get_logger_for_web())
        logging.info(f"Flask 应用启动时获取到 {len(available_voices)} 个可用语音。")
        if not available_voices:
             logging.warning("获取到的可用语音列表为空。请检查 TTS 配置和服务是否可用。")
    except Exception as e:
         logging.error(f"Flask 应用启动时获取可用 TTS 语音列表失败: {e}", exc_info=True)
         available_voices = []
         logging.warning("无法获取可用语音列表，下拉框将为空。")
elif not TTS_MANAGER_AVAILABLE:
    logging.error("TTS 管理器模块 (tts_manager_edge.py) 未能成功导入，语音功能将不可用。")
    available_voices = []


# --- 路由定义 ---

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if not CELERY_AVAILABLE:
             logging.error("收到上传请求，但 Celery 后台系统不可用。")
             flash("服务器后台任务处理系统未启动，无法处理请求。", "error")
             return render_template('index.html', voices=available_voices)

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
            unique_filename = f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_stem}{Path(original_filename).suffix}"
            
            # 保存上传文件到 UPLOAD_FOLDER
            upload_folder_path = Path(app.config['UPLOAD_FOLDER'])
            upload_folder_path.mkdir(parents=True, exist_ok=True) # 确保上传目录存在
            filepath = upload_folder_path / unique_filename


            try:
                file.save(filepath)
                logging.info(f"文件已保存: {filepath}")

                voice_id = request.form.get('voice_id')
                if not voice_id:
                     if available_voices:
                          voice_id = available_voices[0].get('id', 'zh-CN-XiaoxiaoNeural') # 默认语音
                          logging.warning(f"上传请求未收到 voice_id，使用默认值: {voice_id}")
                     else:
                           flash("无法获取语音设置，且没有可用语音列表。请检查后台服务。", "error")
                           logging.error("无法获取 voice_id，且没有可用语音列表。")
                           return render_template('index.html', voices=available_voices)
                
                # 传递给 Celery 任务的是上传文件的绝对路径字符串
                # 和最终输出目录的绝对路径字符串 (FLASK_OUTPUT_BASE_DIR)
                task = convert_ppt_to_video_task.apply_async(args=[
                    str(filepath.resolve()),
                    str(FLASK_OUTPUT_BASE_DIR.resolve()), # 确保传递的是绝对路径
                    voice_id
                ])
                logging.info(f"Celery 任务已发送，任务 ID: {task.id}")
                return redirect(url_for('task_status', task_id=task.id))

            except Exception as e:
                logging.error(f"文件上传或任务发送失败: {e}", exc_info=True)
                flash(f"文件上传或处理启动失败: {e}", "error")
                return render_template('index.html', voices=available_voices)
        else:
            flash(f"只允许上传文件类型为: {', '.join(app.config['ALLOWED_EXTENSIONS'])}", "error")
            return redirect(request.url)

    return render_template('index.html', voices=available_voices)


# --- 新增：语音试听路由 ---
@app.route('/preview_tts/<voice_id>', methods=['GET'])
def preview_tts(voice_id):
    logger = get_logger_for_web()
    if not TTS_MANAGER_AVAILABLE:
        logger.error("TTS 管理器不可用，无法提供试听。")
        return jsonify({"error": "TTS service is unavailable"}), 503

    if not voice_id or voice_id not in KNOWN_EDGE_VOICES: # 检查 voice_id 是否在已知列表中
        logger.warning(f"请求试听的 voice_id 无效: {voice_id}")
        return jsonify({"error": "Invalid voice ID"}), 400

    # 使用一个固定的示例文本进行试听
    # 可以根据 voice_id 的语言选择不同的示例文本
    lang_prefix = KNOWN_EDGE_VOICES[voice_id].get('lang', 'en').split('-')[0].lower()
    sample_text = "你好，这是一个语音试听示例。" if lang_prefix == 'zh' else "Hello, this is a voice preview."
    
    temp_audio_file_path_str = None # 初始化变量

    try:
        logger.info(f"开始为 voice_id '{voice_id}' 生成试听音频...")
        # generate_preview_audio 现在直接从 core_logic.tts_manager_edge 导入
        # 它会在系统临时目录中创建文件
        temp_audio_file_path_str = generate_preview_audio(voice_id, logger, text=sample_text)

        if temp_audio_file_path_str:
            temp_audio_file_path = Path(temp_audio_file_path_str)
            
            # 使用 after_this_request 确保在响应发送后删除临时文件
            @after_this_request
            def cleanup_preview_audio(response):
                try:
                    if temp_audio_file_path.exists():
                        os.remove(temp_audio_file_path)
                        logger.info(f"已删除试听临时文件: {temp_audio_file_path}")
                except Exception as e_clean:
                    logger.error(f"删除试听临时文件 {temp_audio_file_path} 失败: {e_clean}", exc_info=True)
                return response
            
            logger.info(f"成功生成试听音频: {temp_audio_file_path}, 将发送文件。")
            # 发送文件，浏览器会处理为音频流
            return send_file(
                temp_audio_file_path,
                mimetype='audio/mpeg',
                as_attachment=False # 重要：不作为附件下载，而是让浏览器尝试播放在线
            )
        else:
            logger.error(f"为 voice_id '{voice_id}' 生成试听音频失败。generate_preview_audio 返回 None。")
            return jsonify({"error": "Failed to generate preview audio"}), 500

    except Exception as e:
        logger.error(f"生成试听音频时发生意外错误 for voice_id '{voice_id}': {e}", exc_info=True)
        # 如果在 send_file 之前发生错误，并且临时文件已创建，尝试删除
        if temp_audio_file_path_str and Path(temp_audio_file_path_str).exists():
            try:
                os.remove(temp_audio_file_path_str)
                logger.info(f"已在错误处理中删除试听临时文件: {temp_audio_file_path_str}")
            except Exception as e_clean_err:
                logger.error(f"错误处理中删除试听临时文件 {temp_audio_file_path_str} 失败: {e_clean_err}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500


@app.route('/status/<task_id>')
def task_status(task_id):
    if not CELERY_AVAILABLE:
         # flash("服务器后台任务处理系统未启动，无法获取状态。", "error") # flash 消息可能不适合这里
         return render_template('status.html', task_id=task_id, initial_status='ERROR', error_message="服务器后台任务处理系统未启动，无法获取状态。")

    task = celery_app_instance.AsyncResult(task_id)
    initial_meta = task.info if task.info and isinstance(task.info, dict) else {}
    
    # 确保将初始状态和 meta 传递给模板
    # initial_status 可以是 PENDING, STARTED, SUCCESS, FAILURE 等
    return render_template('status.html', task_id=task_id, initial_status=task.state, initial_meta=initial_meta)


@app.route('/tasks/<task_id>/status')
def get_task_status(task_id):
    if not CELERY_AVAILABLE:
         return jsonify({'state': 'ERROR', 'error': 'Celery 后台系统未启动'}), 500

    task = celery_app_instance.AsyncResult(task_id)
    response = {
        'state': task.state,
        'task_id': task.id,
        'meta': task.info if task.info and isinstance(task.info, dict) else {}
    }

    if task.state == 'SUCCESS' or task.state == 'COMPLETE': # 兼容 COMPLETE
        final_result = task.result
        response['result'] = final_result 

        try:
             if final_result:
                 filename_for_download = Path(final_result).name
                 response['download_url'] = url_for('download_file', filename=filename_for_download)
             else:
                  logging.warning(f"任务 {task_id} 成功完成，但返回值为空。无法提供下载链接。")
                  response['error'] = "任务成功完成，但未生成有效的下载链接。"
                  # 即使 Celery 任务是 SUCCESS，但如果没结果，对用户来说是失败
                  response['state'] = 'FAILURE' # 在前端标记为失败
                  if 'meta' not in response: response['meta'] = {}
                  response['meta']['error'] = "任务成功但未返回结果文件路径。"

        except Exception as e:
             logging.error(f"为任务 {task_id} 构造下载 URL 失败: {e}", exc_info=True)
             response['error'] = f"构造下载链接失败: {e}"
             response['state'] = 'FAILURE'
             if 'meta' not in response: response['meta'] = {}
             response['meta']['error'] = f"构造下载链接失败: {str(e)}"


    elif task.state == 'FAILURE':
        # 错误信息通常已经在 meta.error 和 meta.traceback 中了
        # Celery 的 task.result 在失败时是异常对象
        # 我们在 tasks.py 中已经将详细错误放入 meta
        pass

    return jsonify(response)


@app.route('/output/<filename>')
def download_file(filename):
    output_base_dir = FLASK_OUTPUT_BASE_DIR
    logging.info(f"尝试从基础目录 '{output_base_dir}' 提供文件 '{filename}' 下载。")
    safe_filename = secure_filename(filename)
    try:
        full_file_path = output_base_dir / safe_filename
        if not full_file_path.exists():
            logging.error(f"在预期下载路径 '{full_file_path}' 未找到文件。")
            from werkzeug.exceptions import NotFound
            raise NotFound()
        return send_from_directory(output_base_dir, safe_filename, as_attachment=True)
    except FileNotFoundError:
         from werkzeug.exceptions import NotFound
         raise NotFound()
    except Exception as e:
         logging.error(f"处理文件 '{safe_filename}' 下载时发生错误: {e}", exc_info=True)
         return "文件下载失败。", 500


@app.errorhandler(404)
def page_not_found(e):
    logging.warning(f"收到 404 请求: {request.url} (错误: {e})")
    return render_template('404.html'), 404


@app.errorhandler(413) # Werkzeug 上传文件过大错误
def file_too_large(e):
    logging.warning(f"收到文件过大上传请求: {request.url}")
    # 使用 flash 消息，并在 index 模板中显示
    flash(f"上传文件过大，最大允许 {app.config['MAX_CONTENT_LENGTH'] / 1024 / 1024:.2f}MB", "error")
    return redirect(url_for('index')) # 重定向回主页


if __name__ == '__main__':
    # 设置日志级别
    log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(level=log_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # 为 werkzeug 设置日志级别 (Flask 的内置服务器)
    # werkzeug_logger = logging.getLogger('werkzeug')
    # werkzeug_logger.setLevel(log_level) # 可以和应用日志级别一致或不同

    logging.info("在开发模式下运行 Flask 应用...")
    app.run(debug=True, host='0.0.0.0', port=5000)
