# tasks.py
import os
import logging
from celery import Celery
from celery import signals # 导入信号
from celery.utils.log import get_task_logger # 获取任务专属日志记录器
from pathlib import Path
import configparser
import shutil
import time

# 从 celery_app.py 导入 Celery 应用实例
from .celery_app import celery_app

# 导入核心处理逻辑函数
# 使用相对导入，因为 core_logic 是一个包
try:
    from .core_logic.ppt_processor import process_presentation_for_task
    from .core_logic.video_synthesizer import synthesize_video_for_task
    # 导入获取可用语音的函数，可能在 web 前端也需要调用
    from .core_logic.tts_manager_edge import get_available_voices as get_available_tts_voices_core # 导入并重命名
    # 导入 utils 模块，获取工具函数
    from .core_logic.utils import get_tool_path, get_poppler_path, get_audio_duration
except ImportError as e:
    # 如果核心逻辑模块导入失败，整个 worker 将无法启动或任务会失败
    logging.error(f"无法导入核心逻辑模块: {e}")
    # 这里不强制退出，让 Celery 捕获异常
    # raise e # 抛出异常会阻止 worker 启动

# --- 获取任务专属日志记录器 ---
# 这是在 Celery Worker 中进行日志记录的标准方式
task_logger = get_task_logger(__name__)


# --- 定义 Celery 任务 ---
# @celery_app.task 装饰器将一个普通 Python 函数注册为 Celery 任务
# bind=True 表示任务函数将接收第一个参数 'self'，即任务实例，可以访问 retry, request 等属性
# name 可以指定任务名称，不指定则默认使用函数名（包含模块名）
@celery_app.task(bind=True, name='ppt_to_video.convert_task')
def convert_ppt_to_video_task(self, pptx_filepath_str: str, output_dir_str: str, voice_id: str):
    """
    后台 Celery 任务：执行 PPT 到视频的转换。

    Args:
        self: 任务实例。
        pptx_filepath_str: 上传的 PPTX 文件路径（字符串）。
        output_dir_str: 最终视频输出目录路径（字符串）。
        voice_id: 选择的 TTS 语音 ID。

    Returns:
        成功时返回最终视频文件路径（字符串），失败时记录错误并可能由 Celery 标记失败。
    """
    # --- 获取配置和日志记录器 ---
    # 在任务内部重新加载配置是确保最新配置被使用的安全方式
    # 但对于频繁运行的任务，可以考虑缓存配置
    config = configparser.ConfigParser()
    config_path = Path(__file__).parent.parent / 'config.ini' # 定位 config.ini
    if config_path.exists():
        try:
            config.read(config_path, encoding='utf-8')
        except Exception as e:
            task_logger.error(f"任务中加载配置 {config_path} 失败: {e}")
            # 严重错误，标记任务失败
            raise RuntimeError(f"加载配置失败: {e}")

    # 使用任务专属日志记录器
    logger = task_logger

    # 从配置获取临时文件基础目录
    base_temp_dir = Path(config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp')) # 使用 /tmp 作为默认回退
    # 确保临时目录基础路径存在
    try:
         base_temp_dir.mkdir(parents=True, exist_ok=True)
         logger.info(f"任务临时文件基础目录: {base_temp_dir}")
    except OSError as e:
         logger.error(f"无法创建任务临时文件基础目录 {base_temp_dir}: {e}")
         # 无法创建临时目录，任务无法继续
         raise RuntimeError(f"无法创建任务临时文件基础目录: {e}")

    pptx_filepath = Path(pptx_filepath_str)
    output_dir_path = Path(output_dir_str)

    logger.info(f"任务 {self.request.id} 开始处理: {pptx_filepath.name}")
    start_time = time.time()
    temp_run_dir = None # 初始化临时任务目录变量

    try:
        # --- 1. 处理演示文稿 (导出图片、提取备注、生成音频) ---
        logger.info("阶段 1/2: 处理演示文稿...")
        # 将 logger, config 传递给 process_presentation_for_task
        processed_data, temp_run_dir = process_presentation_for_task(
            pptx_filepath,
            base_temp_dir,
            voice_id,
            logger,
            config # 传递 config
        )

        if processed_data is None or temp_run_dir is None:
             # process_presentation_for_task 失败时应该抛出异常，这里是双重检查
             raise RuntimeError("演示文稿处理失败或返回无效数据。请检查详细日志。")

        # 更新任务状态 (可选，如果前端需要更细粒度的状态)
        self.update_state(state='PROCESSING', meta={'stage': 'synthesizing_video', 'progress': 50})

        # --- 2. 合成视频 (拼接、生成字幕、添加字幕) ---
        logger.info("阶段 2/2: 合成视频...")
        final_video_filename = f"{pptx_filepath.stem}_{self.request.id[:8]}.mp4" # 使用任务ID的一部分作为文件名
        final_video_path = output_dir_path / final_video_filename
        # 将 logger, config 传递给 synthesize_video_for_task
        synthesis_success = synthesize_video_for_task(
            processed_data,
            temp_run_dir,
            final_video_path,
            logger,
            config # 传递 config
        )

        if not synthesis_success:
            # synthesize_video_for_task 失败时会返回 False，详细错误已在内部记录
            raise RuntimeError("视频合成失败。请检查详细日志。")

        # --- 任务成功完成 ---
        end_time = time.time()
        duration = end_time - start_time
        logger.info(f"任务 {self.request.id} 成功完成！输出文件: {final_video_path.resolve()}")
        logger.info(f"总耗时: {duration:.2f} 秒")

        # 返回最终视频文件的相对路径（相对于 Web 服务的下载目录）
        # 假定 output_dir_path 是 Web 服务静态文件或下载服务的根目录
        # 或者返回相对于 config.ini 中配置的 base_output_dir 的路径
        base_output_dir = Path(config.get('General', 'base_output_dir', fallback='./output'))
        try:
             relative_output_path = final_video_path.relative_to(base_output_dir)
             # 返回相对路径字符串
             return str(relative_output_path)
        except ValueError:
             # 如果最终输出路径不在 base_output_dir 下，返回完整路径或文件名
             logger.warning(f"最终输出路径 '{final_video_path}' 不在基础输出目录 '{base_output_dir}' 下，返回完整路径。")
             return str(final_video_path.resolve())


    except Exception as e:
        # --- 任务处理失败 ---
        error_msg = f"任务 {self.request.id} 处理失败: {e}"
        logger.error(error_msg, exc_info=True) # 记录完整错误信息和堆栈跟踪

        # 标记任务失败
        # Celery 默认会标记任务失败，这里可以添加额外信息
        self.update_state(state='FAILURE', meta={'error': str(e)})

        # 重新抛出异常，让 Celery 记录到结果后端
        raise e

    finally:
        # --- 清理临时文件 ---
        cleanup_temp = config.getboolean('General', 'cleanup_temp_dir', fallback=True)
        if cleanup_temp and temp_run_dir and temp_run_dir.exists():
            logger.info(f"任务 {self.request.id} 正在清理临时目录: {temp_run_dir}")
            try:
                shutil.rmtree(temp_run_dir)
                logger.info("临时目录已清理。")
            except Exception as clean_e:
                logger.error(f"清理临时目录 {temp_run_dir} 失败: {clean_e}")
        elif temp_run_dir and temp_run_dir.exists():
             logger.info(f"任务 {self.request.id} 临时文件保留在: {temp_run_dir} (cleanup_temp = False)")


# --- 可选：在 worker 启动时执行一些初始化操作 ---
# 例如，检查依赖工具是否存在
@signals.worker_init.connect
def worker_init(**kwargs):
    logger = logging.getLogger('celery') # 获取 celery worker 的日志记录器
    logger.info("Celery worker 正在初始化...")

    # 在 worker 启动时加载一次配置 (也可以在每个任务中加载)
    config = configparser.ConfigParser()
    config_path = Path(__file__).parent.parent / 'config.ini'
    if config_path.exists():
        try:
            config.read(config_path, encoding='utf-8')
            logger.info("Worker 初始化：成功加载配置。")
        except Exception as e:
            logger.error(f"Worker 初始化：加载配置 {config_path} 失败: {e}")
    else:
        logger.warning(f"Worker 初始化：配置未找到: {config_path}")

    # 检查关键外部依赖是否存在
    logger.info("Worker 初始化：检查外部依赖...")
    dependencies_ok = True
    tools_to_check = ["ffmpeg", "ffprobe", "soffice"] # 需要检查的工具列表

    for tool in tools_to_check:
        # 在这里调用 utils 中的 get_tool_path 需要一个临时的 configparser 实例
        # 或者将 config_path 传递给 get_tool_path (修改其签名)
        # 简化：暂时直接在 worker_init 里检查 PATH，或依赖任务内部的检查
        # 更好的做法是：在 tasks 中通过 configparser 和 get_tool_path 检查
        # 这里仅作为worker启动时的提示性检查
        try:
             # 临时创建一个 configparser 来调用 get_tool_path
             temp_config = configparser.ConfigParser()
             if config_path.exists(): temp_config.read(config_path, encoding='utf-8')
             if get_tool_path(tool, logger, temp_config) is None:
                 logger.error(f"Worker 初始化：外部工具 '{tool}' 未找到。依赖于 '{tool}' 的任务可能会失败。")
                 dependencies_ok = False
        except Exception as e:
             logger.error(f"Worker 初始化：检查工具 '{tool}' 时发生错误: {e}")
             dependencies_ok = False

    # 检查 pdf2image 和 opencc 库是否已安装
    if not WHISPER_AVAILABLE: # 检查 stable-ts 是否已导入
        logger.error("Worker 初始化：Python 库 'stable-ts' 未导入。依赖于它的任务将失败。")
        dependencies_ok = False
    if not PILLOW_AVAILABLE: # 检查 Pillow 是否已导入
        logger.error("Worker 初始化：Python 库 'Pillow' 未导入。")
        dependencies_ok = False
    if not OPENCC_AVAILABLE: # 检查 opencc 是否已导入
         logger.warning("Worker 初始化：Python 库 'opencc-python-reimplemented' 未导入。繁简转换将跳过。")
         # 这不是一个硬性依赖，不阻止任务运行

    if dependencies_ok:
        logger.info("Worker 初始化：核心外部依赖检查通过 (工具路径可能需要在 config.ini 中精确配置)。")
    else:
        logger.warning("Worker 初始化：部分外部依赖检查未通过。请确保所有必需的外部工具和 Python 库已安装。")


# --- 获取可用语音列表的函数 ---
# 这个函数通常由 Web 前端调用，所以放在 tasks.py 里不合适，
# 但为了方便，可以先放在这里，并在 app.py 中导入调用。
# 更好的做法是将其移到 core_logic/tts_manager_edge.py 并从那里导入到 app.py。
# 在这里先提供一个壳，实际调用 core_logic 中的函数
def get_available_tts_voices(logger: logging.Logger) -> list[dict]:
    """
    获取可用 TTS 语音列表。

    Args:
        logger: 日志记录器实例。

    Returns:
        语音信息字典列表。
    """
    # 直接调用 core_logic 中的函数
    return get_available_tts_voices_core(logger)