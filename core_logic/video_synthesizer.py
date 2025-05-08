# core_logic/video_synthesizer.py
import os
import logging
import time
import shutil
from pathlib import Path
import subprocess
import shlex # 用于命令行参数引用
import json # 用于解析 ffprobe 输出
import platform # 判断操作系统
import sys # 用于获取 frozen 状态和路径
import configparser # 导入配置解析器
import uuid # 用于生成唯一文件名

# 导入同级模块的工具函数
try:
    from .utils import get_tool_path, get_audio_duration
except ImportError as e:
    logging.error(f"FATAL ERROR: 无法导入 core_logic.utils 模块: {e}")
    raise ImportError(f"无法导入 core_logic.utils 模块: {e}") from e


# 导入 ASR 库
try:
    # stable-ts 包安装后导入时通常用 stable_whisper
    import stable_whisper
    WHISPER_AVAILABLE = True
except ImportError:
    logging.error("FATAL ERROR: 缺少 'stable-ts' 库。请确保环境正确安装和打包包含！")
    WHISPER_AVAILABLE = False # 标记为不可用，后续函数会检查

# 导入繁简转换库 (可选)
try:
    import opencc # opencc-python-reimplemented 安装后导入名
    OPENCC_AVAILABLE = True
except ImportError:
    logging.warning("缺少 'opencc-python-reimplemented' 库，将无法进行繁简转换！")
    OPENCC_AVAILABLE = False # 标记为不可用

# 导入图像库 (用于获取尺寸)
try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    logging.error("FATAL ERROR: 缺少 'Pillow' 库！请确保环境正确安装和打包包含！")
    PILLOW_AVAILABLE = False


# --- 可选：在 worker 启动时执行一些初始化操作 ---
from celery import signals  # Add this import at the top of the file

# --- 定义任务阶段常量 (与 tasks.py 保持一致) ---
STAGE_START = 'Initializing'
STAGE_PPT_PROCESSING = 'Processing Presentation'
STAGE_PPT_IMAGES = 'Exporting Slides'
STAGE_EXTRACT_NOTES = 'Extracting Notes'
STAGE_GENERATE_AUDIO = 'Generating Audio'
STAGE_VIDEO_SYNTHESIS = 'Synthesizing Video' # 视频合成主要阶段
STAGE_VIDEO_SEGMENTS = 'Creating Video Segments' # 子阶段：生成片段
STAGE_VIDEO_CONCAT = 'Concatenating Video' # 子阶段：拼接片段
STAGE_GENERATE_SUBTITLES = 'Generating Subtitles (ASR)' # 子阶段：生成字幕
STAGE_ADD_SUBTITLES = 'Adding Subtitles' # 子阶段：添加字幕
STAGE_CLEANUP = 'Cleaning Up'
STAGE_COMPLETE = 'Complete'


# --- ASR 字幕生成函数 ---
def srt_formatter(result: stable_whisper.WhisperResult, **kwargs) -> str:
    """
    将 stable-ts 结果格式化为 SRT 字符串。
    """
    return result.to_srt_vtt(word_level=False)


def generate_subtitles(
    audio_file_paths: list[str], # 接受有效的音频文件路径列表
    output_srt_path: Path,
    temp_dir: Path, # 任务的临时目录
    logger: logging.Logger, # 日志记录器
    config: configparser.ConfigParser, # 配置对象
    task_instance # <--- 接收任务实例
) -> bool:
    """
    合并有效的音频文件，使用 Whisper 生成 SRT 字幕文件。
    失败时返回 False。
    """
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 0, 'status': 'Starting ASR'})
    logger.debug("开始生成字幕...")
    if not WHISPER_AVAILABLE:
        logger.error("stable-ts 库不可用，无法进行字幕生成。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Error: stable-ts not available'})
        return False

    valid_audio_files = [Path(p) for p in audio_file_paths if p and Path(p).is_file() and Path(p).stat().st_size > 100]

    if not valid_audio_files:
        logger.warning("没有有效的音频文件可用于生成字幕，跳过字幕生成。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 100, 'status': 'No valid audio'})
        return False

    # --- 使用 FFmpeg 合并音频 ---
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 5, 'status': 'Concatenating audio for ASR'})
    concat_list_path = temp_dir / f"audio_concat_list_{uuid.uuid4().hex[:4]}.txt"
    combined_audio_path = temp_dir / f"combined_audio_for_asr_{uuid.uuid4().hex[:4]}.wav"

    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
        logger.error("无法合并音频，因为找不到 ffmpeg。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Error: ffmpeg not found'})
        if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)
        return False

    try:
        with open(concat_list_path, 'w', encoding='utf-8') as f:
            for audio_file_path in valid_audio_files:
                safe_path = str(audio_file_path.resolve()).replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
        logger.debug(f"为 FFmpeg 创建了音频合并列表: {concat_list_path.name}")

        cmd_concat = [
            ffmpeg_path, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_path.resolve()),
            "-c", "copy",
            str(combined_audio_path.resolve())
        ]
        logger.debug(f"执行 FFmpeg 命令合并音频: {shlex.join(cmd_concat)}")
        result = subprocess.run(cmd_concat, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')

        if result.returncode != 0:
            logger.error(f"FFmpeg 合并音频失败。返回码: {result.returncode}")
            logger.error(f"FFmpeg 命令: {shlex.join(cmd_concat)}")
            if result.stdout: logger.error(f"  FFmpeg (concat) STDOUT:\n{result.stdout}")
            if result.stderr: logger.error(f"  FFmpeg (concat) STDERR:\n{result.stderr}")
            if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Error: Audio concatenation failed'})
            return False

        logger.debug("使用 FFmpeg 合并音频完成。")
        if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)

        if not combined_audio_path.exists() or combined_audio_path.stat().st_size < 100:
            logger.error(f"FFmpeg 合并音频后文件无效或为空: {combined_audio_path.name}")
            if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Error: Empty combined audio'})
            return False

        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 10, 'status': 'Audio concatenated'})


    except FileNotFoundError:
        logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Error: ffmpeg not found'})
        if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)
        if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.error(f"合并音频时发生错误: {e}", exc_info=True)
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': f'Error: Audio concatenation failed ({type(e).__name__})'})
        if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)
        if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
        return False

    # --- 运行 Whisper ASR ---
    model = None
    whisper_model_name = config.get('Audio', 'whisper_model', fallback='base')
    logger.info(f"加载 Whisper 模型 '{whisper_model_name}'，并强制使用 CPU...")

    original_tqdm_disable = os.environ.get('TQDM_DISABLE')

    try:
        os.environ['TQDM_DISABLE'] = '1'

        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 15, 'status': 'Loading ASR model'})
        asr_start_time = time.time()
        # 强制 CPU 加载和推理
        model = stable_whisper.load_model(whisper_model_name, device="cpu")
        logger.info(f"已加载 Whisper 模型 '{whisper_model_name}'，实际使用设备: {model.device}")

        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 20, 'status': 'Running ASR'})
        logger.info("开始语音识别 (ASR)...")
        # transcribe 函数本身是同步的，运行在 worker 进程中
        result = model.transcribe(
            str(combined_audio_path),
            fp16=False,
            verbose=True, # 设置 verbose=True，让 whisper 库自己的进度条显示
            # device="cpu", # 确保这里不意外使用 GPU
            # task=self # stable-whisper 或 whisper 库没有直接支持接收 Celery 任务实例进行进度更新
        )
        asr_end_time = time.time()
        logger.info(f"语音识别完成，耗时 {asr_end_time - asr_start_time:.2f} 秒。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 90, 'status': 'ASR complete'})


        logger.debug(f"将结果格式化并保存到 {output_srt_path.name}...")

        # --- 繁简转换 (根据配置决定是否执行) ---
        srt_content = srt_formatter(result)
        enable_opencc = config.getboolean('General', 'enable_opencc', fallback=False)
        if enable_opencc and OPENCC_AVAILABLE:
            try:
                cc = opencc.OpenCC('t2s.json') # Config file name only
                srt_content = cc.convert(srt_content)
                logger.info("成功使用 OpenCC 将字幕内容转换为简体。")
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 92, 'status': 'OpenCC conversion complete'})
            except Exception as e:
                logger.error(f"OpenCC 转换 SRT 内容时出错: {e}。将保存原始 SRT。", exc_info=True)
                task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': f'Warning: OpenCC failed ({type(e).__name__})'})
        elif enable_opencc and not OPENCC_AVAILABLE:
             logger.warning("配置启用了 OpenCC，但库不可用，跳过繁简转换。")
             task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Skipping OpenCC (not available)'})
        else:
             logger.debug("配置禁用了 OpenCC 或库不可用，跳过繁简转换。")
             # task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'OpenCC disabled'})


        with open(output_srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        # 检查生成的 SRT 文件是否包含有效内容
        srt_is_valid = False
        try:
            if output_srt_path.exists() and output_srt_path.stat().st_size > 5:
                 with open(output_srt_path, 'r', encoding='utf-8') as f:
                     for line in f:
                         line_strip = line.strip()
                         if line_strip and '-->' in line_strip or (line_strip and not line_strip.isdigit()):
                             srt_is_valid = True
                             break
                 if srt_is_valid:
                      logger.info("生成的 SRT 字幕文件包含有效文本。")
                      task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 95, 'status': 'SRT file generated'})
                 else:
                      logger.warning("生成的 SRT 文件为空或不包含有效文本内容。")
                      output_srt_path.unlink(missing_ok=True)
                      task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Error: SRT file invalid or empty'})
            else:
                 logger.warning("生成的 SRT 文件过小或为空。")
                 if output_srt_path.exists(): output_srt_path.unlink(missing_ok=True)
                 task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': 'Error: SRT file too small or empty'})
        except Exception as e:
             logger.error(f"检查生成的 SRT 文件有效性时出错: {e}", exc_info=True)
             if output_srt_path.exists(): output_srt_path.unlink(missing_ok=True)
             task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': f'Error: SRT validation failed ({type(e).__name__})'})


        if not srt_is_valid:
             return False # 字幕生成失败

        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 100, 'status': 'Subtitles generated successfully'})
        return True # 字幕生成成功

    except Exception as e:
        logger.error(f"运行 Whisper ASR 或保存字幕时出错: {e}", exc_info=True)
        if output_srt_path.exists(): output_srt_path.unlink(missing_ok=True)
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'status': f'Error: ASR failed ({type(e).__name__})'})
        return False
    finally:
        if original_tqdm_disable is None:
            if 'TQDM_DISABLE' in os.environ: del os.environ['TQDM_DISABLE']
        else:
            os.environ['TQDM_DISABLE'] = original_tqdm_disable

        if model is not None:
             logger.debug("尝试释放 Whisper 模型内存...")
             del model


# --- FFmpeg 核心功能函数 ---

def create_video_segment(
    image_path: Path,
    duration: float,
    audio_path: Path | None,
    output_path: Path,
    logger: logging.Logger,
    config: configparser.ConfigParser,
    task_instance # <--- 接收任务实例
) -> bool:
    """
    使用 FFmpeg 将单张图片转换为指定时长的视频片段，并附加音频。失败时返回 False。
    """
    logger.debug(f"  使用 FFmpeg 创建视频片段: {output_path.name} (目标时长: {duration:.3f}s)")
    # task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'status': f'Creating segment for {output_path.name}'})


    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
         logger.error("FFmpeg 路径未解析，无法创建视频片段。")
         # task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'status': 'Error: ffmpeg not found'})
         return False
    if not image_path.is_file():
         logger.error(f"图片文件不存在: {image_path}")
         return False
    if duration <= 0:
        logger.warning(f"目标时长无效 ({duration:.3f}s)，跳过创建片段 {output_path.name}。")
        return False

    target_width = config.getint('Video', 'target_width', fallback=1280)
    target_fps = config.getint('Video', 'target_fps', fallback=24)

    temp_video_path = output_path.with_suffix(".temp_video_" + uuid.uuid4().hex[:4] + ".mp4")
    step1_success = False

    cmd_step1 = [
        ffmpeg_path, "-y",
        "-loop", "1", "-framerate", str(target_fps),
        "-i", str(image_path.resolve()),
        "-vf", f"scale={target_width}:-2:force_original_aspect_ratio=decrease,pad={target_width}:{target_width*9//16}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,fps={target_fps}",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an", str(temp_video_path.resolve())
    ]
    try:
        logger.debug(f"    执行 FFmpeg 命令 (步骤 1 - 图片转无声视频): {shlex.join(cmd_step1)}")
        result1 = subprocess.run(cmd_step1, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
        if result1.returncode != 0:
            logger.error(f"  FFmpeg 创建无声视频失败: {temp_video_path.name}。返回码: {result1.returncode}")
            logger.error(f"  FFmpeg 命令: {shlex.join(cmd_step1)}")
            if result1.stdout: logger.error(f"  FFmpeg (step1) STDOUT:\n{result1.stdout}")
            if result1.stderr: logger.error(f"  FFmpeg (step1) STDERR:\n{result1.stderr}")
            if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
            # task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'status': f'Error creating segment {output_path.name} ({result1.returncode})'})
            return False
        logger.debug(f"    步骤 1 成功: 已生成无声视频 {temp_video_path.name}")
        step1_success = True
    except FileNotFoundError:
        logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
        return False
    except Exception as e:
        logger.error(f"  创建无声视频时发生未知错误 {temp_video_path.name}: {e}", exc_info=True)
        if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
        return False

    if step1_success:
        audio_is_valid = audio_path and audio_path.is_file() and audio_path.stat().st_size > 100
        if audio_is_valid:
            logger.debug(f"    步骤 2: 合并视频与音频 {audio_path.name} 到 {output_path.name}")
            cmd_step2 = [
                ffmpeg_path, "-y",
                "-i", str(temp_video_path.resolve()),
                "-i", str(audio_path.resolve()),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                str(output_path.resolve())
            ]
            try:
                logger.debug(f"    执行 FFmpeg 命令 (步骤 2 - 合并音视频): {shlex.join(cmd_step2)}")
                result2 = subprocess.run(cmd_step2, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
                if result2.returncode != 0:
                    logger.error(f"  FFmpeg 合并音视频失败: {output_path.name}。返回码: {result2.returncode}")
                    logger.error(f"  FFmpeg 命令: {shlex.join(cmd_step2)}")
                    if result2.stdout: logger.error(f"  FFmpeg (step2) STDOUT:\n{result2.stdout}")
                    if result2.stderr: logger.error(f"  FFmpeg (step2) STDERR:\n{result2.stderr}")
                    if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
                    if output_path.exists(): output_path.unlink(missing_ok=True)
                    # task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'status': f'Error merging audio for {output_path.name} ({result2.returncode})'})
                    return False
                logger.debug(f"    步骤 2 成功: 已合并音视频到 {output_path.name}")
                if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
                return True
            except FileNotFoundError:
                 logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
                 if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
                 return False
            except Exception as e:
                 logger.error(f"  合并音视频时发生未知错误 {output_path.name}: {e}", exc_info=True)
                 if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
                 if output_path.exists(): output_path.unlink(missing_ok=True)
                 return False
        else:
            logger.debug(f"    步骤 2: 无有效音频，直接使用无声视频 {temp_video_path.name} 作为输出 {output_path.name}")
            try:
                shutil.move(str(temp_video_path.resolve()), str(output_path.resolve()))
                return True
            except Exception as e:
                 logger.error(f"    重命名/移动无声视频失败: {e}", exc_info=True)
                 if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
                 if output_path.exists(): output_path.unlink(missing_ok=True)
                 return False

    return False


def concatenate_videos(video_file_paths: list[Path], output_path: Path, logger: logging.Logger, config: configparser.ConfigParser, task_instance) -> bool:
    """
    使用 FFmpeg concat demuxer 拼接视频文件列表。失败时返回 False。
    """
    logger.debug(f"使用 FFmpeg concat demuxer 拼接视频 ({len(video_file_paths)} 段)...")
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'progress': 50, 'status': 'Starting concatenation'})

    if not video_file_paths:
        logger.warning("要拼接的视频列表为空。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'status': 'Error: No segments to concatenate'})
        return False

    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
        logger.error("FFmpeg 路径未解析，无法拼接视频。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'status': 'Error: ffmpeg not found'})
        return False

    concat_list_file = output_path.parent / f"concat_list_{uuid.uuid4().hex[:4]}.txt"
    try:
        with open(concat_list_file, 'w', encoding='utf-8') as f:
            for video_file in video_file_paths:
                 if video_file.is_file():
                    safe_path = str(video_file.resolve()).replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")
                 else:
                     logger.warning(f"要拼接的视频文件不存在，已跳过: {video_file}")
        if concat_list_file.stat().st_size == 0:
             logger.error("生成的拼接列表文件为空，没有有效视频可拼接。")
             concat_list_file.unlink(missing_ok=True)
             task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'status': 'Error: Concat list empty'})
             return False

        logger.debug(f"创建了拼接列表文件: {concat_list_file.name}")

        cmd_list = [
            ffmpeg_path, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_file.resolve()),
            "-c", "copy", # 直接复制代码流（包括视频和音频），速度快
            str(output_path.resolve())
        ]
        logger.debug(f"  执行 FFmpeg 命令: {shlex.join(cmd_list)}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'progress': 60, 'status': 'Running FFmpeg concat'})
        result = subprocess.run(cmd_list, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')

        if result.returncode != 0:
            logger.error(f"FFmpeg 拼接视频失败。返回码: {result.returncode}")
            logger.error(f"FFmpeg 命令: {shlex.join(cmd_list)}")
            if result.stdout: logger.error(f"  FFmpeg (concat) STDOUT:\n{result.stdout}")
            if result.stderr: logger.error(f"  FFmpeg (concat) STDERR:\n{result.stderr}")
            if output_path.exists(): output_path.unlink(missing_ok=True)
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'status': 'Error: FFmpeg concat failed', 'ffmpeg_stderr': result.stderr})
            return False

        logger.debug(f"视频拼接成功: {output_path.name}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'progress': 70, 'status': 'Concatenation complete'})
        return True

    except FileNotFoundError:
         logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
         task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'status': 'Error: ffmpeg not found'})
         return False
    except Exception as e:
         logger.error(f"创建拼接列表或执行拼接时发生错误: {e}", exc_info=True)
         if output_path.exists(): output_path.unlink(missing_ok=True)
         return False
    finally:
         if concat_list_file.exists():
             try: concat_list_file.unlink()
             except OSError: pass


def add_subtitles(input_video: Path, srt_file: Path, output_video: Path, logger: logging.Logger, config: configparser.ConfigParser, task_instance) -> bool:
    """
    使用 FFmpeg 将 SRT 字幕硬编码到视频中。失败时返回 False。
    """
    logger.debug(f"使用 FFmpeg 添加字幕到视频 '{input_video.name}'...")
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'progress': 90, 'status': 'Starting subtitle embedding'})


    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
         logger.error("FFmpeg 路径未解析，无法添加字幕。")
         task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Error: ffmpeg not found'})
         return False
    if not input_video.is_file():
         logger.error(f"输入视频文件不存在: {input_video}")
         task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Error: Input video not found'})
         return False
    if not srt_file.is_file():
         logger.error(f"字幕文件不存在: {srt_file}")
         task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Error: SRT file not found'})
         return False
    if srt_file.stat().st_size == 0:
        logger.warning(f"字幕文件为空: {srt_file}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Warning: SRT file empty'})
        return False # SRT 文件为空也视为失败

    # 从 config.ini 读取字幕样式
    ffmpeg_style_str = config.get(
        'Video',
        'subtitle_style_ffmpeg',
        fallback="Fontname=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,BackColour=&H9A000000,BorderStyle=1,Outline=1,Shadow=0.8,Alignment=2,MarginV=25"
    )
    logger.debug(f"使用的字幕样式 (force_style): {ffmpeg_style_str}")

    # --- 构建正确的 filtergraph 字符串 ---
    # SRT 文件路径需要正确引用给 libass
    srt_path_str = str(srt_file.resolve())
    # 在 filtergraph 字符串内部，单引号需要 \' 转义
    filter_srt_path_escaped = srt_path_str.replace("'", r"\'")

    # 将样式字符串内部的单引号也转义
    styles_escaped = ffmpeg_style_str.replace("'", r"\'")

    # --- 构建正确的 filtergraph 字符串 ---
    # 使用 subtitles 滤镜，filename 选项和 force_style 选项
    # 格式: subtitles=filename='...',force_style='...'
    # **选项之间用冒号 : 分隔**
    # 每个选项的值用单引号包裹，内部单引号用 \' 转义

    # 构造滤镜选项字符串
    # 核心： filename='...',force_style='...'
    filter_options = f"filename='{filter_srt_path_escaped}':force_style='{styles_escaped}'" # <--- 关键：选项之间用冒号 :

    # 完整的 -vf 参数值就是 "subtitles=filename='...':force_style='...'"
    # 将滤镜名称和参数用等号连接
    vf_param_value = f"subtitles={filter_options}"


    # **或者尝试 ass 滤镜，语法类似，有时更稳定**
    # filter_name_ass = "ass"
    # filter_options_ass = f"filename='{filter_srt_path_escaped}':force_style='{styles_escaped}'"
    # vf_param_value = f"{filter_name_ass}={filter_options_ass}"


    input_video_str = str(input_video.resolve())
    output_video_str = str(output_video.resolve())

    # --- 构建 FFmpeg 命令 ---
    cmd_list = [
        ffmpeg_path, "-y", # 覆盖输出文件
        "-i", input_video_str, # 输入视频
        "-vf", vf_param_value, # <--- 使用构建好的滤镜参数字符串
        "-c:v", "libx264", # 视频编码器
        "-preset", "medium", # 编码速度
        "-crf", "22", # 质量因子
        "-c:a", "copy", # 直接复制音频流
        str(output_video.resolve()) # 输出文件
    ]
    try:
        logger.debug(f"  执行 FFmpeg 命令 (添加字幕): {shlex.join(cmd_list)}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'progress': 92, 'status': 'Running FFmpeg subtitles'})

        result = subprocess.run(cmd_list, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')

        if result.returncode != 0:
            logger.error(f"FFmpeg 添加字幕失败。返回码: {result.returncode}")
            logger.error(f"FFmpeg 命令: {shlex.join(cmd_list)}")
            if result.stdout: logger.error(f"  FFmpeg (subtitles) STDOUT:\n{result.stdout}")
            if result.stderr: logger.error(f"  FFmpeg (subtitles) STDERR:\n{result.stderr}")
            if output_video.exists(): output_video.unlink(missing_ok=True)
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Error: FFmpeg subtitles failed', 'ffmpeg_stderr': result.stderr})
            return False # 添加字幕失败

        logger.debug(f"字幕添加成功: {output_video.name}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'progress': 100, 'status': 'Subtitles added successfully'})
        return True # 字幕添加成功

    except FileNotFoundError:
         logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
         task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Error: ffmpeg not found'})
         return False
    except Exception as e:
         logger.error(f"添加字幕时发生未知错误: {e}", exc_info=True)
         if output_video.exists(): output_video.unlink(missing_ok=True)
         task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': f'Error: Adding subtitles failed ({type(e).__name__})'})
         return False


# --- 视频合成主函数 (由 Celery 任务调用) ---
def synthesize_video_for_task(
    processed_data: list[dict],
    temp_run_dir: Path, # 任务的临时目录
    output_video_path_base: Path, # 最终视频输出路径 (Task ID 和扩展名已包含在内)
    logger: logging.Logger,
    config: configparser.ConfigParser,
    task_instance # <--- 接收任务实例
) -> bool:
    """
    根据处理好的数据，使用 FFmpeg 合成最终视频。
    包括：生成视频片段、拼接、生成字幕、添加字幕。
    失败时返回 False，成功时返回 True。
    """
    logger.info("--- 开始基于 FFmpeg 的视频合成流程 ---")
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SYNTHESIS, 'progress': 50, 'status': 'Starting synthesis'}) # 更新主要阶段状态

    if not processed_data:
        logger.error("输入数据为空，无法合成视频。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SYNTHESIS, 'status': 'Error: No data to synthesize'})
        return False

    # 最终输出文件路径
    final_video_path = output_video_path_base # 直接使用传入的完整路径

    # 确保最终输出目录存在
    try:
        final_video_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"确保最终输出目录存在: {final_video_path.parent}")
    except OSError as e:
        logger.error(f"无法创建最终输出目录 {final_video_path.parent}: {e}")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SYNTHESIS, 'status': f'Error: Cannot create output directory ({type(e).__name__})'})
        return False

    temp_segments_dir = temp_run_dir / "video_segments"
    try:
        temp_segments_dir.mkdir(exist_ok=True)
    except OSError as e:
         logger.error(f"无法创建视频片段临时目录 {temp_segments_dir}: {e}")
         task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'status': f'Error: Cannot create temp directory ({type(e).__name__})'})
         return False


    segment_files = []
    default_slide_duration = config.getfloat('Video', 'default_slide_duration', fallback=3.0)
    num_slides_to_process = len(processed_data)

    # --- 1. 生成各幻灯片的视频片段 ---
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'progress': 52, 'status': 'Creating segments'}) # 更新阶段和起始进度
    logger.info(f"步骤 1/3: 使用 FFmpeg 生成各幻灯片的视频片段 ({num_slides_to_process} 个)")
    for i, data in enumerate(processed_data):
        slide_num = data.get('slide_number', i + 1)
        image_path_str = data.get('image_path')
        audio_path_str = data.get('audio_path')
        audio_duration = data.get('audio_duration', 0.0)

        if not image_path_str or not Path(image_path_str).is_file():
            logger.warning(f"幻灯片 {slide_num}: 图片路径无效或文件不存在 '{image_path_str}'。跳过此片段。")
            continue

        image_path = Path(image_path_str)
        audio_path = Path(audio_path_str) if audio_path_str and Path(audio_path_str).is_file() else None

        clip_duration = audio_duration if audio_duration is not None and audio_duration > 0.01 else default_slide_duration

        segment_output_path = temp_segments_dir / f"segment_{slide_num}_{uuid.uuid4().hex[:4]}.mp4"

        success = create_video_segment(
            image_path,
            clip_duration,
            audio_path if audio_duration is not None and audio_duration > 0.01 else None,
            segment_output_path,
            logger,
            config,
            task_instance # <--- 传递任务实例
        )
        if success:
            segment_files.append(segment_output_path)
            progress = 52 + int((i + 1) / num_slides_to_process * 15) # 52% 到 67% 用于片段生成
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'progress': progress, 'current_slide': slide_num})
        else:
            logger.error(f"未能创建幻灯片 {slide_num} 的视频片段。合成中止。")
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'status': f'Error creating segment for slide {slide_num}'})
            return False # 任一片段失败，整个合成失败


    if not segment_files:
        logger.error("未能成功生成任何视频片段。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'status': 'Error: No segments created'})
        return False

    task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_SEGMENTS, 'progress': 67, 'status': 'Segments created'})


    # --- 2. 拼接视频片段 ---
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'progress': 68, 'status': 'Concatenating segments'})
    logger.info(f"步骤 2/3: 使用 FFmpeg concat demuxer 拼接视频片段 ({len(segment_files)} 个)")
    base_video_path = temp_run_dir / f"base_video_no_subs_{uuid.uuid4().hex[:4]}.mp4"
    success_concat = concatenate_videos(segment_files, base_video_path, logger, config, task_instance)
    if not success_concat:
        logger.error("拼接视频片段失败。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'status': 'Error: FFmpeg concat failed'})
        return False

    task_instance.update_state('PROCESSING', meta={'stage': STAGE_VIDEO_CONCAT, 'progress': 70, 'status': 'Concatenation complete'})


    # --- 3. 生成字幕 ---
    task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 72, 'status': 'Generating subtitles (ASR)'})
    logger.info("步骤 3/3: 生成字幕文件 (ASR) 并添加到视频")
    audio_paths_for_asr = [d.get('audio_path') for d in processed_data if d.get('audio_path') and d.get('audio_duration', 0) > 0.01]
    subtitle_file_path = temp_run_dir / "subtitles.srt" # <--- 使用固定文件名
    subtitles_generated = False
    asr_errors_occurred = False

    if audio_paths_for_asr:
        logger.info(f"发现 {len(audio_paths_for_asr)} 个有效音频片段用于 ASR。")
        try:
             subtitles_generated = generate_subtitles(
                audio_paths_for_asr,
                subtitle_file_path,
                temp_run_dir, # 传递临时目录
                logger,
                config,
                task_instance # <--- 传递任务实例
             )
        except Exception as asr_e:
             logger.error(f"调用 generate_subtitles 时发生错误: {asr_e}", exc_info=True)
             asr_errors_occurred = True
             subtitles_generated = False

    else:
        logger.info("没有有效时长的音频文件，跳过字幕生成。")
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_GENERATE_SUBTITLES, 'progress': 100, 'status': 'Skipping ASR (no audio)'})


    # 检查 SRT 文件有效性
    srt_is_valid = subtitles_generated and subtitle_file_path.exists() and subtitle_file_path.stat().st_size > 5

    # --- 添加字幕 (如果成功生成) ---
    if srt_is_valid:
        task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'progress': 90, 'status': 'Adding subtitles'})
        logger.info("字幕文件有效，尝试添加字幕到视频。")
        final_video_with_subs_path = final_video_path # 最终输出路径

        success_sub = add_subtitles(base_video_path, subtitle_file_path, final_video_with_subs_path, logger, config, task_instance) # <--- 传递任务实例

        if success_sub:
            logger.debug(f"字幕添加成功。最终视频已保存到: {final_video_with_subs_path.resolve()}")
            if base_video_path.exists(): base_video_path.unlink(missing_ok=True)
            if subtitle_file_path.exists(): subtitle_file_path.unlink(missing_ok=True)
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'progress': 100, 'status': 'Subtitles added successfully'})
            return True # 整个合成流程成功

        else:
            logger.error("添加字幕失败。将输出不带字幕的视频。")
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Error: FFmpeg subtitles failed', 'ffmpeg_stderr': 'Check logs for details'}) # 更新状态
            # 添加字幕失败，将基础无字幕视频移动到最终输出位置
            try:
                 shutil.move(str(base_video_path.resolve()), str(final_video_path.resolve()))
                 logger.warning(f"最终视频 (无字幕 - 因添加失败) 已保存到: {final_video_path.resolve()}")
                 if subtitle_file_path.exists(): subtitle_file_path.unlink(missing_ok=True)
                 task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'progress': 100, 'status': 'Warning: Subtitles failed, saved video without subtitles'})
                 return True # 视为成功 (有视频输出)
            except Exception as e:
                 logger.error(f"移动最终无字幕视频时出错: {e}", exc_info=True)
                 task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': f'Error: Failed to move base video ({type(e).__name__})'})
                 return False # 移动失败，任务失败

    else:
        # 如果 SRT 文件无效或生成失败
        logger.warning("步骤 4: 跳过添加字幕 (字幕文件无效或生成失败)。将输出不带字幕的视频。")
        if asr_errors_occurred:
            logger.error("字幕生成过程中发生了错误，请检查日志。")
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Error: ASR failed or SRT invalid, skipping subtitles'})
        else:
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Skipping subtitles (no valid SRT)'})


        # 将基础无字幕视频移动到最终输出位置
        # 检查 base_video_path 是否存在 (前面拼接可能失败)
        if base_video_path.exists():
            try:
                 shutil.move(str(base_video_path.resolve()), str(final_video_path.resolve()))
                 logger.info(f"最终视频 (无字幕) 已保存到: {final_video_path.resolve()}")
                 if subtitle_file_path.exists(): subtitle_file_path.unlink(missing_ok=True)
                 task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'progress': 100, 'status': 'Saved video without subtitles'})
                 return True # 视为成功 (有视频输出)
            except Exception as e:
                 logger.error(f"移动最终无字幕视频时出错: {e}", exc_info=True)
                 task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': f'Error: Failed to move base video ({type(e).__name__})'})
                 return False
        else:
            logger.error("基础视频文件不存在，无法保存无字幕视频。")
            task_instance.update_state('PROCESSING', meta={'stage': STAGE_ADD_SUBTITLES, 'status': 'Fatal Error: Base video not found'})
            return False


    # 理论上代码不会执行到这里
    logger.error("视频合成函数执行流程异常结束，未返回明确状态。")
    task_instance.update_state('PROCESSING', meta={'stage': 'Synthesis Logic Error', 'status': 'Error: Unexpected end of function'})
    return False



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
    try:
        from .tts_manager_edge import get_available_tts_voices_core
        return get_available_tts_voices_core(logger)
    except ImportError as e:
        logger.error(f"FATAL ERROR: 无法导入 get_available_tts_voices_core: {e}")
        raise ImportError(f"无法导入 get_available_tts_voices_core: {e}") from e