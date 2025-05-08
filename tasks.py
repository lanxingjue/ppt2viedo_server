# tasks.py
import os
import logging
from celery import Celery
from celery import signals
from celery.utils.log import get_task_logger # 获取任务专属日志记录器
from pathlib import Path
import configparser
import shutil
import time
import sys
import traceback # 用于获取堆栈跟踪

# --- 从 celery_app.py 文件导入 Celery 应用实例 (绝对导入) ---
# 假定 celery_app.py 文件在项目根目录
try:
    from celery_app import celery_app
except ImportError as e:
    # 如果导入失败，记录并抛出异常，让 Celery 报告加载错误
    logging.error(f"FATAL ERROR: 无法导入 celery_app 模块: {e}")
    raise ImportError(f"无法导入 celery_app 模块: {e}") from e


# --- 导入核心处理逻辑函数 (绝对导入) ---
# 假定 core_logic 目录在项目根目录
try:
    from core_logic.ppt_processor import process_presentation_for_task
    from core_logic.video_synthesizer import synthesize_video_for_task
    from core_logic.tts_manager_edge import get_available_voices as get_available_tts_voices_core # 导入并重命名
    from core_logic.utils import get_tool_path, get_poppler_path, get_audio_duration # 导入 utils
except ImportError as e:
    # 如果核心逻辑模块导入失败，整个 worker 将无法启动或任务会失败
    logging.error(f"FATAL ERROR: 无法导入核心逻辑模块或其依赖: {e}")
    raise ImportError(f"无法导入核心逻辑模块或其依赖: {e}") from e

# --- 获取任务专属日志记录器 ---
task_logger = get_task_logger(__name__)


# --- 定义任务阶段常量 (用于状态更新) ---
# 这些常量在 core_logic/*.py 中也定义，确保一致
STAGE_START = 'Initializing'
STAGE_PPT_PROCESSING = 'Processing Presentation'
STAGE_PPT_IMAGES = 'Exporting Slides'
STAGE_EXTRACT_NOTES = 'Extracting Notes'
STAGE_GENERATE_AUDIO = 'Generating Audio'
STAGE_VIDEO_SYNTHESIS = 'Synthesizing Video'
STAGE_VIDEO_SEGMENTS = 'Creating Video Segments'
STAGE_VIDEO_CONCAT = 'Concatenating Video'
STAGE_GENERATE_SUBTITLES = 'Generating Subtitles (ASR)'
STAGE_ADD_SUBTITLES = 'Adding Subtitles'
STAGE_CLEANUP = 'Cleaning Up'
STAGE_COMPLETE = 'Complete'


# --- 定义 Celery 任务 ---
@celery_app.task(bind=True, name='ppt_to_video.convert_task')
def convert_ppt_to_video_task(self, pptx_filepath_str: str, output_dir_str: str, voice_id: str):
    """
    后台 Celery 任务：执行 PPT 到视频的转换。
    增加状态更新和错误信息处理。
    """
    task_id = self.request.id
    # 在任务开始时立即更新状态
    self.update_state(state='STARTED', meta={'stage': STAGE_START, 'progress': 0})
    logger = task_logger

    logger.info(f"任务 {task_id} 开始处理: {pptx_filepath_str}")
    start_time = time.time()
    temp_run_dir = None # 初始化临时任务目录变量

    try:
        # --- 1. 准备工作和环境 ---
        self.update_state(state='PROCESSING', meta={'stage': STAGE_START, 'progress': 5})
        # 获取配置和日志记录器
        config = configparser.ConfigParser()
        config_path = Path(__file__).parent.parent / 'config.ini'
        if config_path.exists():
            try:
                config.read(config_path, encoding='utf-8')
            except Exception as e:
                error_msg = f"任务 {task_id} 加载配置 {config_path} 失败: {e}"
                logger.error(error_msg, exc_info=True)
                # 任务内部的配置加载失败是严重错误，直接标记失败
                self.update_state(state='FAILURE', meta={'error': error_msg, 'exc_type': type(e).__name__, 'traceback': traceback.format_exc(), 'stage': STAGE_START})
                raise RuntimeError(error_msg) from e

        # 从配置获取临时文件基础目录并确保存在
        base_temp_dir = Path(config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp'))
        try:
             base_temp_dir.mkdir(parents=True, exist_ok=True)
             logger.info(f"任务 {task_id} 临时文件基础目录: {base_temp_dir}")
        except OSError as e:
             error_msg = f"任务 {task_id} 无法创建任务临时文件基础目录 {base_temp_dir}: {e}"
             logger.error(error_msg, exc_info=True)
             self.update_state(state='FAILURE', meta={'error': error_msg, 'exc_type': type(e).__name__, 'traceback': traceback.format_exc(), 'stage': STAGE_START})
             raise RuntimeError(error_msg) from e


        pptx_filepath = Path(pptx_filepath_str)
        output_dir_path = Path(output_dir_str) # 最终输出目录（来自 config 的绝对路径）


        # --- 2. 处理演示文稿 (导出图片、提取备注、生成音频) ---
        # 在每个主要阶段开始时更新状态
        self.update_state(state='PROCESSING', meta={'stage': STAGE_PPT_PROCESSING, 'progress': 10})
        logger.info("阶段 1/2: 处理演示文稿 (导出、备注、音频)...")
        try:
            # 调用 process_presentation_for_task，传递 logger, config, 以及任务实例 self
            # process_presentation_for_task 内部也应发送更细粒度的状态
            processed_data, temp_run_dir = process_presentation_for_task(
                pptx_filepath,
                base_temp_dir, # 传递临时目录基础路径
                voice_id,
                logger,
                config, # 传递 config
                self # <--- 在这里传递任务实例 self
            )

            if processed_data is None or temp_run_dir is None:
                 # process_presentation_for_task 失败时应该抛出异常，这里是双重检查
                 raise RuntimeError("演示文稿处理失败或返回无效数据。请检查详细日志。")

        except Exception as e:
            # 捕获 process_presentation_for_task 抛出的异常
            error_msg = f"任务 {task_id} 演示文稿处理步骤发生错误: {e}"
            logger.error(error_msg, exc_info=True)
            # 在更新状态时包含错误信息和当前阶段
            # 如果在 process_presentation_for_task 内部已经 update_state 标记了失败状态和阶段，这里会覆盖
            self.update_state(state='FAILURE', meta={'error': error_msg, 'exc_type': type(e).__name__, 'traceback': traceback.format_exc(), 'stage': STAGE_PPT_PROCESSING})
            raise RuntimeError(error_msg) from e # 重新抛出异常

        # process_presentation_for_task 成功完成后的进度更新
        self.update_state(state='PROCESSING', meta={'stage': STAGE_PPT_PROCESSING, 'progress': 45, 'status': 'Presentation processing complete'})


        # --- 3. 合成视频 (拼接、生成字幕、添加字幕) ---
        # 在第二个主要阶段开始时更新状态
        self.update_state(state='PROCESSING', meta={'stage': STAGE_VIDEO_SYNTHESIS, 'progress': 50})
        logger.info("阶段 2/2: 合成视频 (拼接、字幕)...")

        # 确保最终输出目录存在且有权限 (这一步在开始时和这里都检查了，双重保险)
        try:
             output_dir_path.mkdir(parents=True, exist_ok=True)
             logger.debug(f"确保最终输出目录存在: {output_dir_path}")
        except OSError as e:
             error_msg = f"任务 {task_id} 无法创建最终输出目录 {output_dir_path}: {e}"
             logger.error(error_msg, exc_info=True)
             self.update_state(state='FAILURE', meta={'error': error_msg, 'exc_type': type(e).__name__, 'traceback': traceback.format_exc(), 'stage': STAGE_VIDEO_SYNTHESIS})
             raise RuntimeError(error_msg) from e


        # 构建最终视频文件路径 (包含任务ID和扩展名)
        final_video_filename = f"{pptx_filepath.stem}_{task_id[:8]}.mp4"
        final_video_full_path = output_dir_path / final_video_filename # 这是最终视频的完整路径

        try:
            # 调用 synthesize_video_for_task，传递 logger, config, 以及任务实例 self
            # synthesize_video_for_task 内部会发送更细粒度的状态更新
            synthesis_success = synthesize_video_for_task(
                processed_data,
                temp_run_dir, # 传递任务的临时目录
                final_video_full_path, # <--- 传递最终视频的完整路径给 synthesize_video_for_task
                logger,
                config, # 传递 config
                self # <--- 传递任务实例 self
            )

            if not synthesis_success:
                # synthesize_video_for_task 失败时会返回 False，详细错误应已在内部记录
                # 抛出异常，让 Celery 标记任务失败
                raise RuntimeError("视频合成失败。请检查详细日志。")

        except Exception as e:
            # 捕获 synthesize_video_for_task 抛出的异常
            error_msg = f"任务 {task_id} 视频合成步骤发生错误: {e}"
            logger.error(error_msg, exc_info=True)
            # 在更新状态时包含错误信息和当前阶段
            # 如果在 synthesize_video_for_task 内部已经 update_state 标记了失败状态和阶段，这里会覆盖
            self.update_state(state='FAILURE', meta={'error': error_msg, 'exc_type': type(e).__name__, 'traceback': traceback.format_exc(), 'stage': STAGE_VIDEO_SYNTHESIS})
            raise RuntimeError(error_msg) from e


        # --- 任务成功完成 ---
        end_time = time.time()
        duration = end_time - start_time
        logger.info(f"任务 {task_id} 成功完成！最终输出文件: {final_video_full_path.resolve()}")
        logger.info(f"总耗时: {duration:.2f} 秒")

        # 更新最终状态为 SUCCESS
        # 返回结果（最终视频路径相对于基础输出目录）
        # config 中 base_output_dir 是 Web 服务下载的根目录
        base_output_dir_config = Path(config.get('General', 'base_output_dir', fallback='./output'))
        try:
             # 计算最终视频文件相对于配置的 base_output_dir 的路径
             relative_output_path = final_video_full_path.relative_to(base_output_dir_config)
             final_result_path_str = str(relative_output_path)
        except ValueError:
             # 如果最终输出路径不在 config 中配置的基础输出目录 下
             # 返回文件名部分，假定 Flask download_file 路由能处理
             logger.warning(f"最终输出路径 '{final_video_full_path}' 不在配置的基础输出目录 '{base_output_dir_config}' 下，返回文件名。")
             final_result_path_str = final_video_full_path.name # 返回文件名即可，方便前端构造下载 URL


        # 更新最终状态为 SUCCESS，包含输出路径和总耗时
        self.update_state(state=STAGE_COMPLETE, meta={
            'stage': STAGE_COMPLETE, # 阶段
            'progress': 100, # 进度
            'output_path': final_result_path_str, # 返回路径字符串
            'duration': duration # 总耗时
        })
        return final_result_path_str # 返回结果字符串给 Celery Backend

    except Exception as e:
        # --- 捕获所有未被特定阶段捕获的异常 ---
        # 这通常发生在阶段之间的代码或 finally 块之前
        error_msg = f"任务 {task_id} 处理过程中发生未被捕获的错误: {e}"
        logger.error(error_msg, exc_info=True)

        # 标记任务失败，并在 meta 中包含错误信息、异常类型和堆栈跟踪
        # 如果在前面的 try 块中已经 update_state 标记了失败状态和阶段，这里不会再次触发
        # 如果是未被特定阶段捕获的错误，就使用通用的 FAILURE 状态
        current_meta = self.request.get_current_task().info # 获取当前状态信息
        if current_meta and 'stage' in current_meta:
             # 如果已经有阶段信息，只补充错误详情
             self.update_state(
                 state='FAILURE',
                 meta={
                     **current_meta, # 保留原有的 meta 信息
                     'error': str(e), # 异常的字符串表示
                     'exc_type': type(e).__name__, # 异常类型名称
                     'traceback': traceback.format_exc() # 完整的堆栈跟踪
                 }
             )
        else:
            # 如果没有阶段信息，说明在阶段之间或初始化时失败
            self.update_state(
                state='FAILURE',
                meta={
                    'error': str(e),
                    'exc_type': type(e).__name__,
                    'traceback': traceback.format_exc(),
                    'stage': 'Unhandled Error' # 添加一个通用阶段
                }
            )

        # 重新抛出异常，让 Celery Backend 记录到结果存储中
        raise e # 抛出异常非常重要，否则 Celery 会认为任务成功


    finally:
        # --- 清理临时文件 ---
        # 在清理阶段开始时更新状态 (可能会在失败时也执行)
        self.update_state(state='PROCESSING', meta={'stage': STAGE_CLEANUP, 'progress': 98})
        logger.info(f"任务 {task_id} 的 finally 块开始执行清理。")
        cleanup_temp = config.getboolean('General', 'cleanup_temp_dir', fallback=True)
        # 只有当 temp_run_dir 变量在 try 块中被成功赋值后才尝试清理
        if cleanup_temp and temp_run_dir and temp_run_dir.exists():
            logger.info(f"任务 {task_id} 正在清理临时目录: {temp_run_dir}")
            try:
                shutil.rmtree(temp_run_dir)
                logger.info("临时目录已清理。")
            except Exception as clean_e:
                logger.error(f"清理临时目录 {temp_run_dir} 失败: {clean_e}", exc_info=True)
        elif temp_run_dir and temp_run_dir.exists():
             logger.info(f"任务 {task_id} 临时文件保留于: {temp_run_dir} (cleanup_temp = False)")


# --- 可选：在 worker 启动时执行一些初始化操作 ---
# ... (worker_init 函数保持不变) ...
@signals.worker_init.connect
def worker_init(**kwargs):
    logger = logging.getLogger('celery')
    logger.info("Celery worker 正在初始化...")

    config = configparser.ConfigParser()
    config_path = Path(__file__).parent.parent / 'config.ini'
    if config_path.exists():
        try:
            config.read(config_path, encoding='utf-8')
            logger.info("Worker 初始化：成功加载配置。")
        except Exception as e:
            logger.error(f"Worker 初始化：加载配置 {config_path} 失败: {e}", exc_info=True)

    logger.info("Worker 初始化：检查外部依赖工具...")
    dependencies_ok = True
    tools_to_check = ["ffmpeg", "ffprobe", "soffice"]

    temp_config_for_check = configparser.ConfigParser()
    if config_path.exists(): temp_config_for_check.read(config_path, encoding='utf-8')

    for tool in tools_to_check:
        try:
             if get_tool_path(tool, logger, temp_config_for_check) is None:
                 logger.error(f"Worker 初始化：外部工具 '{tool}' 未找到。依赖于 '{tool}' 的任务可能会失败。")
                 dependencies_ok = False
        except Exception as e:
             logger.error(f"Worker 初始化：检查工具 '{tool}' 时发生错误: {e}", exc_info=True)
             dependencies_ok = False

    python_libs_check_ok = True
    libs_to_check = ['core_logic.ppt_processor', 'core_logic.video_synthesizer', 'core_logic.tts_manager_edge', 'core_logic.utils', 'stable_whisper', 'PIL', 'opencc']
    for lib_name in libs_to_check:
         try:
              import importlib
              importlib.import_module(lib_name)
              logger.info(f"Worker 初始化：Python 模块 '{lib_name}' 导入成功。")
         except ImportError:
              logger.error(f"Worker 初始化：Python 库/模块 '{lib_name}' 未导入。依赖于它的任务将失败。")
              python_libs_check_ok = False
         except Exception as e:
              logger.error(f"Worker 初始化：导入 Python 模块 '{lib_name}' 时发生意外错误: {e}", exc_info=True)
              python_libs_check_ok = False


    if dependencies_ok and python_libs_check_ok:
        logger.info("Worker 初始化：核心外部依赖和 Python 库检查通过。Worker 就绪。")
    else:
        logger.error("Worker 初始化：部分外部依赖或 Python 库检查未通过。请确保所有必需的软件和库已安装。")


# --- 获取可用语音列表的函数 ---
# 这个函数通常由 Web 前端调用，需要从 tasks 模块导入到 app.py
def get_available_tts_voices(logger: logging.Logger) -> list[dict]:
    """
    获取可用 TTS 语音列表。
    """
    # 直接调用 core_logic 中的函数
    return get_available_tts_voices_core(logger)