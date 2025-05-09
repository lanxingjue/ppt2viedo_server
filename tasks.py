# tasks.py
import os
import logging
from celery import Celery, signals
from celery.utils.log import get_task_logger
from pathlib import Path
import configparser
import shutil
import time
import sys
import traceback
from datetime import datetime

# --- Celery 应用实例 ---
# celery_app 实例应该由 celery_app.py 提供并在这里导入
# 或者，如果 tasks.py 是 Celery 的 include 目标，Celery 会处理任务注册
# 我们假设 celery_app.py 中定义的 celery_app 是可用于注册任务的
try:
    from celery_app import celery_app # 从 celery_app.py 导入 Celery 实例
except ImportError:
    # 如果直接运行 tasks.py 或在测试环境中，celery_app 可能未定义
    # 创建一个临时的，主要为了代码能解析，实际 worker 启动时会用 celery_app.py 中的
    logging.warning("无法从 celery_app 导入 celery_app 实例，将使用临时实例。这在 worker 运行时应能正常工作。")
    celery_app = Celery('ppt2video_tasks_fallback_in_tasks')


# --- Flask App 和 DB 导入 ---
# 这些导入现在应该可以工作了，因为 celery_app.py 修改了 sys.path
# 这些变量将在任务执行时，在 app_context 内被使用
flask_app_instance = None
db_instance = None
User_model = None
TaskRecord_model = None
app_config_instance = None # 用于存储解析的 configparser 对象

try:
    from app import app as flask_app_imported, db as db_imported # 从 app.py 导入
    from models import User as User_imported, TaskRecord as TaskRecord_imported # 从 models.py 导入
    
    flask_app_instance = flask_app_imported
    db_instance = db_imported
    User_model = User_imported
    TaskRecord_model = TaskRecord_imported
    app_config_instance = flask_app_instance.config.get('APP_CONFIG') # 获取存储在 Flask app 配置中的 configparser 对象
    
    if app_config_instance is None:
        logging.error("CRITICAL: ConfigParser 对象未在 Flask app.config['APP_CONFIG'] 中找到！Celery 任务可能无法正确获取配置。")
        # 可以尝试在任务内部重新加载 config.ini 作为备用方案，但这不理想

    FLASK_CONTEXT_AVAILABLE = True
    logging.info("tasks.py: Flask app, db, models, 和 app_config 成功导入/设置。")

except ImportError as e:
    logging.error(f"CRITICAL: tasks.py 无法导入 Flask app, db, models, 或 app_config: {e}. 依赖数据库的任务将失败。", exc_info=True)
    FLASK_CONTEXT_AVAILABLE = False
except AttributeError as e_attr: # 例如，如果 app.config['APP_CONFIG'] 不存在
    logging.error(f"CRITICAL: tasks.py 初始化时 AttributeError (可能是 APP_CONFIG 未设置): {e_attr}. 依赖配置的任务将失败。", exc_info=True)
    FLASK_CONTEXT_AVAILABLE = False


# --- 核心逻辑导入 ---
try:
    from core_logic.ppt_processor import process_presentation_for_task
    from core_logic.video_synthesizer import synthesize_video_for_task
    from core_logic.tts_manager_edge import get_available_voices as get_available_tts_voices_core
    # from core_logic.utils import get_tool_path, get_poppler_path, get_audio_duration # 这些通常在 core_logic 内部调用
except ImportError as e:
    logging.error(f"FATAL ERROR: 无法导入核心逻辑模块: {e}", exc_info=True)
    # 这会阻止任务的正确执行

task_logger = get_task_logger(__name__)

# --- 任务阶段常量 ---
STAGE_START = 'Initializing'
STAGE_PPT_PROCESSING = 'Processing Presentation'
STAGE_VIDEO_SYNTHESIS = 'Synthesizing Video'
STAGE_CLEANUP = 'Cleaning Up'
STAGE_COMPLETE = 'Complete'
STAGE_DB_UPDATE = 'Updating Database'


@celery_app.task(bind=True, name='ppt_to_video.convert_task', acks_late=True, reject_on_worker_lost=True,
                  time_limit=3600, soft_time_limit=3500) # 增加超时限制 (1小时硬限制, 略短的软限制)
def convert_ppt_to_video_task(self, pptx_filepath_str: str, output_dir_str: str, voice_id: str, 
                              task_record_id: int, user_id: int):
    task_celery_id = self.request.id
    logger = task_logger # 使用 Celery 的 task_logger
    logger.info(f"Celery 任务 {task_celery_id} (DB Record ID: {task_record_id}) 开始，用户: {user_id}, 文件: {Path(pptx_filepath_str).name}")
    start_time = time.time()
    temp_run_dir = None
    final_video_relative_path = None

    if not FLASK_CONTEXT_AVAILABLE or not flask_app_instance or not db_instance or not User_model or not TaskRecord_model or not app_config_instance:
        error_msg = "任务启动失败：Flask 应用上下文或数据库/模型/配置不可用。"
        logger.error(f"任务 {task_celery_id}: {error_msg}")
        self.update_state(state='FAILURE', meta={'error': error_msg, 'stage': STAGE_START, 'exc_type': 'SetupError'})
        # 不需要显式 raise，Celery 会根据状态处理
        return # 终止任务

    # 使用 with flask_app_instance.app_context() 来确保数据库等操作在正确的上下文中执行
    with flask_app_instance.app_context():
        task_record = None
        try:
            task_record = db_instance.session.get(TaskRecord_model, task_record_id)
            if not task_record:
                error_msg = f'数据库记录 TaskRecord ID {task_record_id} 未找到。'
                logger.error(f"任务 {task_celery_id}: {error_msg}")
                self.update_state(state='FAILURE', meta={'error': error_msg, 'stage': STAGE_START, 'exc_type': 'DBRecordNotFound'})
                return

            if task_record.celery_task_id != task_celery_id and task_record.celery_task_id.startswith("TEMP_"):
                 logger.info(f"任务 {task_celery_id}: 更新 TaskRecord {task_record_id} 的 Celery ID 从 {task_record.celery_task_id} 到 {task_celery_id}.")
                 task_record.celery_task_id = task_celery_id
            
            task_record.status = 'PROCESSING'
            db_instance.session.commit()
            logger.info(f"任务 {task_celery_id}: TaskRecord {task_record_id} 状态更新为 PROCESSING。")
            self.update_state(state='STARTED', meta={'stage': STAGE_START, 'progress': 0})

            # --- 配置和路径准备 ---
            # app_config_instance 是从 Flask app.config 中获取的 configparser 对象
            current_task_config = app_config_instance 
            base_temp_dir = Path(current_task_config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp'))
            base_temp_dir.mkdir(parents=True, exist_ok=True)

            pptx_filepath = Path(pptx_filepath_str)
            output_base_path = Path(output_dir_str)
            safe_original_stem = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in pptx_filepath.stem)
            
            # 使用 TaskRecord ID 来确保文件名的高度唯一性，并与数据库记录关联
            # 这样即使原始文件名相同，不同任务的输出文件也不同
            unique_video_filename = f"{safe_original_stem}_task{task_record_id}_{task_celery_id[:8]}.mp4"
            final_video_full_path = output_base_path / unique_video_filename
            final_video_relative_path = unique_video_filename # 这个将存储在数据库并返回

            task_record.output_video_filename = final_video_relative_path # 预先记录
            db_instance.session.commit()

            # --- 核心处理 ---
            self.update_state(state='PROCESSING', meta={'stage': STAGE_PPT_PROCESSING, 'progress': 10})
            processed_data, temp_run_dir = process_presentation_for_task(
                pptx_filepath, base_temp_dir, voice_id, logger, current_task_config, self
            )
            self.update_state(state='PROCESSING', meta={'stage': STAGE_PPT_PROCESSING, 'progress': 45})

            self.update_state(state='PROCESSING', meta={'stage': STAGE_VIDEO_SYNTHESIS, 'progress': 50})
            synthesis_success = synthesize_video_for_task(
                processed_data, temp_run_dir, final_video_full_path, logger, current_task_config, self
            )

            if not synthesis_success:
                raise RuntimeError("视频合成步骤返回失败。")

            # --- 任务成功 ---
            end_time = time.time()
            duration = end_time - start_time
            logger.info(f"任务 {task_celery_id} 成功！输出: {final_video_full_path.resolve()}，耗时: {duration:.2f}s")
            self.update_state(state='PROCESSING', meta={'stage': STAGE_DB_UPDATE, 'progress': 95, 'status': '更新数据库记录...'})
            
            task_record.status = 'SUCCESS'
            task_record.output_video_filename = final_video_relative_path # 确认文件名
            task_record.completed_at = datetime.utcnow()
            
            user = db_instance.session.get(User_model, user_id)
            if user:
                if user.role == 'free':
                    user.increment_video_count()
                db_instance.session.add(user) # 确保更改被暂存
                logger.info(f"用户 {user.username} ({user.role}) 视频计数更新为: {user.videos_created_count}")
            else:
                logger.warning(f"任务 {task_celery_id}: 未找到用户 ID {user_id} 来更新视频计数。")
            
            db_instance.session.commit()
            logger.info(f"任务 {task_celery_id}: TaskRecord {task_record_id} 和 User {user_id} 已更新。")

            self.update_state(state='SUCCESS', meta={
                'stage': STAGE_COMPLETE, 'progress': 100,
                'output_path': str(final_video_relative_path), 'duration': duration
            })
            return str(final_video_relative_path)

        except Exception as e:
            detailed_traceback = traceback.format_exc()
            error_msg_for_user = f"任务处理失败: {type(e).__name__} - {str(e)[:200]}" # 给用户看的简短错误
            error_msg_for_db = f"{type(e).__name__}: {str(e)}\n\nTraceback:\n{detailed_traceback}"
            logger.error(f"任务 {task_celery_id} (DB Record: {task_record_id}) 失败: {e}", exc_info=True)

            current_meta_for_celery = {}
            try: # 尝试获取当前 meta，如果失败则使用空字典
                current_meta_for_celery = self.request.get_current_task().info or {}
            except Exception:
                logger.warning(f"任务 {task_celery_id}: 获取当前任务 meta 失败。")


            self.update_state(state='FAILURE', meta={
                **current_meta_for_celery,
                'error': error_msg_for_user, # 用户可见的错误
                'exc_type': type(e).__name__,
                'traceback': detailed_traceback, # 详细堆栈给开发者
                'stage': current_meta_for_celery.get('stage', 'Unhandled Error in Task')
            })

            if task_record:
                task_record.status = 'FAILURE'
                task_record.error_message = error_msg_for_db[:5000] # 限制长度以防数据库溢出
                task_record.completed_at = datetime.utcnow()
                try:
                    db_instance.session.commit()
                except Exception as db_error:
                    logger.error(f"任务 {task_celery_id}: 更新 TaskRecord {task_record_id} 为 FAILURE 时数据库错误: {db_error}", exc_info=True)
                    db_instance.session.rollback()
            return # Celery 会知道任务失败了

        finally:
            logger.info(f"任务 {task_celery_id}: Finally 块开始执行清理。")
            # 使用 current_task_config (从 flask_app.config['APP_CONFIG'] 获取)
            cleanup_temp = current_task_config.getboolean('General', 'cleanup_temp_dir', fallback=True)
            if temp_run_dir and temp_run_dir.exists():
                if cleanup_temp:
                    logger.info(f"任务 {task_celery_id}: 清理临时目录: {temp_run_dir}")
                    try:
                        shutil.rmtree(temp_run_dir)
                        logger.info(f"任务 {task_celery_id}: 临时目录已清理。")
                    except Exception as clean_e:
                        logger.error(f"任务 {task_celery_id}: 清理临时目录 {temp_run_dir} 失败: {clean_e}", exc_info=True)
                else:
                    logger.info(f"任务 {task_celery_id}: 临时文件保留于: {temp_run_dir} (cleanup_temp_dir=False)")
            elif temp_run_dir:
                 logger.info(f"任务 {task_celery_id}: 临时目录 {temp_run_dir} 未找到，无需清理。")


# --- 获取可用语音列表的函数 (供 app.py 调用) ---
def get_available_tts_voices(logger: logging.Logger) -> list[dict]:
    return get_available_tts_voices_core(logger)


# --- Worker 初始化信号处理 ---
@signals.worker_init.connect
def worker_init_handler(**kwargs):
    worker_logger = logging.getLogger('celery.worker.init')
    worker_logger.info("Celery worker 正在初始化 (tasks.py worker_init_handler)...")
    # 可以在这里做一些不依赖 Flask app context 的全局初始化
    # 例如，加载一些全局配置或模型（如果它们不直接操作数据库）
    # 确保 FLASK_CONTEXT_AVAILABLE 检查在需要时进行
    if not FLASK_CONTEXT_AVAILABLE:
        worker_logger.critical("Flask 应用上下文在 worker 初始化时不可用。依赖数据库的任务可能无法正确执行。")
    else:
        worker_logger.info("Flask 应用上下文在 worker 初始化时似乎可用。")
    worker_logger.info("Worker 初始化完成。")

