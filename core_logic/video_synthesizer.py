# core_logic/video_synthesizer.py
import configparser # <--- 确保这行在文件的最顶部
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
import uuid # 用于生成唯一文件名
# 导入同级模块的工具函数
from .utils import get_tool_path, get_audio_duration # 导入工具函数

# 导入 ASR 库
try:
    # stable-ts 包安装后导入时通常用 stable_whisper
    import stable_whisper
    WHISPER_AVAILABLE = True
except ImportError:
    # 这个库是必需的，如果在 Celery worker 中仍然导入失败，说明打包或环境有问题
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


# --- ASR 字幕生成函数 ---
def srt_formatter(result: stable_whisper.WhisperResult, **kwargs) -> str:
    """
    将 stable-ts 结果格式化为 SRT 字符串。

    Args:
        result: stable_whisper 转录结果对象。
        **kwargs: 其他可能的格式化参数 (目前未使用)。

    Returns:
        格式化后的 SRT 字符串。
    """
    # 使用 stable-ts 内置的 SRT 格式化功能
    # word_level=False 表示生成句子或短语级别的字幕，而不是每个词一个时间戳
    return result.to_srt_vtt(word_level=False)


def generate_subtitles(
    audio_file_paths: list[str], # 接受有效的音频文件路径列表
    output_srt_path: Path,
    temp_dir: Path, # 任务的临时目录
    logger: logging.Logger, # 日志记录器
    config: configparser.ConfigParser # 配置对象
) -> bool:
    """
    合并有效的音频文件，使用 Whisper 生成 SRT 字幕文件。
    失败时返回 False。

    Args:
        audio_file_paths: 包含需要合并进行 ASR 的音频文件路径的列表。
        output_srt_path: 生成的 SRT 文件保存路径 (Path 对象)。
        temp_dir: 任务的临时工作目录 (Path 对象)。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        bool: SRT 字幕文件生成成功返回 True，否则返回 False。
    """
    logger.info("开始生成字幕...")
    if not WHISPER_AVAILABLE:
        logger.error("stable-ts 库不可用，无法进行字幕生成。")
        return False

    valid_audio_files = [Path(p) for p in audio_file_paths if p and Path(p).is_file() and Path(p).stat().st_size > 100]

    if not valid_audio_files:
        logger.warning("没有有效的音频文件可用于生成字幕，跳过字幕生成。")
        return False

    # --- 使用 FFmpeg 合并音频 ---
    # 需要一个临时的文件列表供 FFmpeg concat demuxer 使用
    concat_list_path = temp_dir / "audio_concat_list.txt"
    combined_audio_path = temp_dir / "combined_audio_for_asr.wav" # 合并后的音频文件 (WAV 格式通常对 ASR 友好)

    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
        logger.error("无法合并音频，因为找不到 ffmpeg。")
        return False

    try:
        # 创建一个包含所有输入音频文件路径的文本文件 (safe way)
        with open(concat_list_path, 'w', encoding='utf-8') as f:
            for audio_file_path in valid_audio_files:
                # FFmpeg concat demuxer 需要特定的格式 'file 'path'', 并处理特殊字符
                # 使用 shlex.quote 应该可以正确引用路径，但对于文件列表，直接使用单引号可能更常见且需要内部转义
                # 更保险的方法是自己手动转义或确保路径不含特殊字符
                # 简化处理：假设路径不含会导致问题的特殊字符，或依赖 shlex.quote (但在文件列表模式下可能不是最佳)
                # 使用 FFmpeg 的 -safe 0 通常允许更多路径格式
                safe_path = str(audio_file_path.resolve()).replace("'", "'\\''") # 基本转义单引号
                f.write(f"file '{safe_path}'\n")
        logger.info(f"为 FFmpeg 创建了音频合并列表: {concat_list_path.name}")

        # FFmpeg 命令：使用 concat demuxer 拼接音频
        cmd_concat = [
            ffmpeg_path, "-y", # 覆盖输出文件
            "-f", "concat",      # 使用 concat demuxer
            "-safe", "0",       # 禁用安全检查 (允许相对/绝对路径)
            "-i", str(concat_list_path.resolve()), # 输入列表文件
            "-c", "copy",       # 直接复制音频流，不重新编码 (速度快，但要求输入音频格式兼容)
            # 如果输入音频格式不兼容，可能需要重新编码，例如:
            # "-c:a", "pcm_s16le", # 转为 PCM 16-bit little-endian WAV
            # "-ar", "16000",       # 设置采样率 (ASR 模型常用 16kHz)
            str(combined_audio_path.resolve()) # 输出合并后的文件
        ]
        logger.info(f"执行 FFmpeg 命令合并音频: {' '.join(shlex.quote(c) for c in cmd_concat)}")
        # 使用 subprocess.run 并捕获输出
        result = subprocess.run(cmd_concat, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')

        # 检查 FFmpeg 返回码
        if result.returncode != 0:
            logger.error(f"FFmpeg 合并音频失败。返回码: {result.returncode}")
            logger.error(f"FFmpeg 命令: {shlex.join(cmd_concat)}")
            if result.stdout: logger.error(f"FFmpeg STDOUT:\n{result.stdout}")
            if result.stderr: logger.error(f"FFmpeg STDERR:\n{result.stderr}")
            # 清理可能产生的输出文件和列表文件
            if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
            if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)
            return False

        logger.info("使用 FFmpeg 合并音频完成。")
        # 清理列表文件
        if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)

        # 检查合并后的音频文件是否存在且有效
        if not combined_audio_path.exists() or combined_audio_path.stat().st_size < 100:
            logger.error(f"FFmpeg 合并音频后文件无效或为空: {combined_audio_path.name}")
            if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
            return False

    except FileNotFoundError:
        logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
        if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)
        if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.error(f"合并音频时发生错误: {e}", exc_info=True)
        if concat_list_path.exists(): concat_list_path.unlink(missing_ok=True)
        if combined_audio_path.exists(): combined_audio_path.unlink(missing_ok=True)
        return False

    # --- 运行 Whisper ASR ---
    model = None # 初始化模型变量
    whisper_model_name = config.get('Audio', 'whisper_model', fallback='base')
    logger.info(f"加载 Whisper 模型 '{whisper_model_name}'...")

    original_tqdm_disable = os.environ.get('TQDM_DISABLE') # 保存原始值

    try:
        # 在调用 Whisper 前设置环境变量禁用 TQDM 进度条
        # 避免在日志中输出进度条干扰
        os.environ['TQDM_DISABLE'] = '1'

        asr_start_time = time.time()
        # 加载模型，PyInstaller Hook 应该处理了模型的捆绑或下载
        # download_root 参数可以指定模型下载路径，默认是 ~/.cache/whisper
        # model = stable_whisper.load_model(whisper_model_name)
        # 尝试强制 CPU 加载和推理
        model = stable_whisper.load_model(whisper_model_name, device="cpu") # <--- 尝试指定 device="cpu"
        logger.info(f"已加载 Whisper 模型 '{whisper_model_name}'，使用设备: {model.device}") # 记录实际设备

        logger.info("开始语音识别 (ASR)...")

        # 执行转录，verbose=False 减少输出
        # language 参数可以尝试指定（如 'zh'），或让 Whisper 自动检测 (更灵活)
        result = model.transcribe(
            str(combined_audio_path),
            fp16=False, # 通常设为 False 以支持更多 CPU 或非 CUDA GPU
            verbose=False,
            # language='zh', # 可选，如果确定语言
            # 尝试在 transcribe 中也指定 device
            # device="cpu" # <--- 尝试在这里也指定
        )
        asr_end_time = time.time()
        logger.info(f"语音识别完成，耗时 {asr_end_time - asr_start_time:.2f} 秒。")

        logger.info(f"将结果格式化并保存到 {output_srt_path.name}...")

        # --- 繁简转换 (如果 opencc 可用) ---
        srt_content = srt_formatter(result)
        if OPENCC_AVAILABLE:
            try:
                cc = opencc.OpenCC('t2s.json') # 创建转换器 (繁体 -> 简体)
                srt_content = cc.convert(srt_content) # 执行转换
                logger.info("成功使用 OpenCC 将字幕内容转换为简体。")
            except Exception as e:
                logger.error(f"OpenCC 转换 SRT 内容时出错: {e}。")
        else:
            logger.warning("由于 opencc-python-reimplemented 未安装，跳过繁简转换。")
        # -------------------------------------

        # 保存 SRT 内容
        with open(output_srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        # 检查生成的 SRT 文件是否包含有效内容（如时间戳行或文本行）
        srt_is_valid = False
        try:
            if output_srt_path.exists() and output_srt_path.stat().st_size > 5: # 检查文件大小
                 with open(output_srt_path, 'r', encoding='utf-8') as f:
                     for line in f:
                         line_strip = line.strip()
                         # 查找包含时间戳或非纯数字行的内容
                         if line_strip and '-->' in line_strip or (line_strip and not line_strip.isdigit()):
                             srt_is_valid = True
                             break
                 if srt_is_valid:
                      logger.info("生成的 SRT 字幕文件包含有效文本。")
                      return True # ASR 和 SRT 生成成功
                 else:
                      logger.warning("生成的 SRT 文件为空或不包含有效文本内容。")
                      output_srt_path.unlink(missing_ok=True) # 删除无效文件
                      return False # 视为失败
            else:
                 logger.warning("生成的 SRT 文件过小或为空。")
                 if output_srt_path.exists(): output_srt_path.unlink(missing_ok=True)
                 return False # 视为失败
        except Exception as e:
             logger.error(f"检查生成的 SRT 文件有效性时出错: {e}", exc_info=True)
             if output_srt_path.exists(): output_srt_path.unlink(missing_ok=True)
             return False # 视为失败


    except Exception as e:
        logger.error(f"运行 Whisper ASR 或保存字幕时出错: {e}", exc_info=True)
        if output_srt_path.exists(): output_srt_path.unlink(missing_ok=True)
        return False
    finally:
        # 恢复原始 TQDM_DISABLE 环境变量值
        if original_tqdm_disable is None:
            if 'TQDM_DISABLE' in os.environ: del os.environ['TQDM_DISABLE']
        else:
            os.environ['TQDM_DISABLE'] = original_tqdm_disable

        if model is not None:
             # 尝试释放模型内存
             logger.debug("尝试释放 Whisper 模型内存...")
             del model
             # 可能需要额外的清理或在 Celery Worker 配置中管理资源


# --- FFmpeg 核心功能函数 ---

def create_video_segment(
    image_path: Path,
    duration: float,
    audio_path: Path | None, # 可以为 None
    output_path: Path,
    logger: logging.Logger, # 日志记录器
    config: configparser.ConfigParser # 配置对象
) -> bool:
    """
    使用 FFmpeg 将单张图片转换为指定时长的视频片段，并附加音频（如果提供且有效）。
    失败时返回 False。

    Args:
        image_path: 输入图片文件的 Path 对象。
        duration: 输出视频片段的目标时长（秒）。
        audio_path: 输入音频文件的 Path 对象，或 None。
        output_path: 输出视频文件的 Path 对象。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        bool: 视频片段生成成功返回 True，否则返回 False。
    """
    logger.info(f"  使用 FFmpeg 创建视频片段: {output_path.name} (目标时长: {duration:.3f}s)")

    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
         logger.error("FFmpeg 路径未解析，无法创建视频片段。")
         return False
    if not image_path.is_file():
         logger.error(f"图片文件不存在: {image_path}")
         return False
    if duration <= 0:
        logger.warning(f"目标时长无效 ({duration:.3f}s)，跳过创建片段 {output_path.name}。")
        return False # 时长无效不生成文件

    # 从配置读取目标尺寸和帧率
    target_width = config.getint('Video', 'target_width', fallback=1280)
    target_fps = config.getint('Video', 'target_fps', fallback=24)

    temp_video_path = output_path.with_suffix(".temp_video.mp4") # 临时的无声视频文件
    step1_success = False

    # --- 步骤 1: 图片转为无声视频 ---
    # -loop 1 循环图片
    # -framerate 设置输入图片帧率
    # -t 设置输出时长
    # -vf 视频滤镜：缩放、填充黑边以保持纵横比、格式转换、设置输出帧率
    # -c:v libx264 视频编码器
    # -preset veryfast 编码速度（影响压缩率和速度）
    # -crf 23 质量因子（越小质量越高，文件越大）
    # -pix_fmt yuv420p 像素格式 (兼容性好)
    # -an 移除音频流 (确保是无声视频)
    # -y 覆盖输出文件
    # -i 输入文件
    cmd_step1 = [
        ffmpeg_path, "-y",
        "-loop", "1", "-framerate", str(target_fps),
        "-i", str(image_path.resolve()),
        "-vf", f"scale={target_width}:-2:force_original_aspect_ratio=decrease,pad={target_width}:{target_width*9//16}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,fps={target_fps}",
        "-t", f"{duration:.3f}", # 使用传入的时长
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an", str(temp_video_path.resolve())
    ]
    try:
        logger.debug(f"    执行 FFmpeg 命令 (步骤 1 - 图片转无声视频): {shlex.join(cmd_step1)}")
        # 设置 stderr=subprocess.PIPE 可以捕获 FFmpeg 输出到 stderr 的日志
        result1 = subprocess.run(cmd_step1, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
        if result1.returncode != 0:
            logger.error(f"  FFmpeg 创建无声视频失败: {temp_video_path.name}。返回码: {result1.returncode}")
            logger.error(f"  FFmpeg 命令: {shlex.join(cmd_step1)}")
            if result1.stdout: logger.error(f"  FFmpeg (step1) STDOUT:\n{result1.stdout}")
            if result1.stderr: logger.error(f"  FFmpeg (step1) STDERR:\n{result1.stderr}")
            if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
            return False
        logger.info(f"    步骤 1 成功: 已生成无声视频 {temp_video_path.name}")
        step1_success = True
    except FileNotFoundError:
        logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
        return False
    except Exception as e:
        logger.error(f"  创建无声视频时发生未知错误 {temp_video_path.name}: {e}", exc_info=True)
        if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
        return False

    # --- 步骤 2: 合并无声视频和音频 (如果音频存在且有效) ---
    # 只有步骤 1 成功才进行
    if step1_success:
        audio_is_valid = audio_path and audio_path.is_file() and audio_path.stat().st_size > 100
        if audio_is_valid:
            logger.info(f"    步骤 2: 合并视频与音频 {audio_path.name} 到 {output_path.name}")
            # -i 输入视频
            # -i 输入音频
            # -c:v copy 直接复制视频流
            # -c:a aac 转码音频为 AAC (兼容性好)
            # -b:a 128k 设置音频比特率
            # -shortest 确保输出时长以最短的输入流为准
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
                    # 清理临时视频和可能产生的输出文件
                    if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
                    if output_path.exists(): output_path.unlink(missing_ok=True)
                    return False
                logger.info(f"    步骤 2 成功: 已合并音视频到 {output_path.name}")
                # 清理临时无声视频
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
            # 如果没有有效的音频，直接将无声视频作为输出文件
            logger.info(f"    步骤 2: 无有效音频，直接使用无声视频 {temp_video_path.name} 作为输出 {output_path.name}")
            try:
                shutil.move(str(temp_video_path.resolve()), str(output_path.resolve()))
                return True
            except Exception as e:
                 logger.error(f"    重命名/移动无声视频失败: {e}", exc_info=True)
                 if temp_video_path.exists(): temp_video_path.unlink(missing_ok=True)
                 if output_path.exists(): output_path.unlink(missing_ok=True) # 如果重命名失败可能残留
                 return False

    return False # 如果步骤 1 失败，直接返回 False


def concatenate_videos(video_file_paths: list[Path], output_path: Path, logger: logging.Logger, config: configparser.ConfigParser) -> bool:
    """
    使用 FFmpeg concat demuxer 拼接视频文件列表。
    失败时返回 False。

    Args:
        video_file_paths: 要拼接的视频文件的 Path 对象列表。
        output_path: 拼接后输出视频文件的 Path 对象。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        bool: 视频拼接成功返回 True，否则返回 False。
    """
    logger.info(f"使用 FFmpeg concat demuxer 拼接视频 ({len(video_file_paths)} 段)...")
    if not video_file_paths:
        logger.warning("要拼接的视频列表为空。")
        return False

    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
        logger.error("FFmpeg 路径未解析，无法拼接视频。")
        return False

    # 创建一个包含所有输入视频文件路径的文本文件 (FFmpeg concat demuxer 需要)
    concat_list_file = output_path.parent / f"concat_list_{uuid.uuid4().hex[:8]}.txt" # 使用唯一文件名避免冲突
    try:
        with open(concat_list_file, 'w', encoding='utf-8') as f:
            for video_file in video_file_paths:
                 if video_file.is_file(): # 只添加存在的有效文件
                    # 使用 resolve 获取绝对路径，并转义单引号
                    safe_path = str(video_file.resolve()).replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")
                 else:
                     logger.warning(f"要拼接的视频文件不存在，已跳过: {video_file}")
        # 检查列表文件是否为空
        if concat_list_file.stat().st_size == 0:
             logger.error("生成的拼接列表文件为空，没有有效视频可拼接。")
             concat_list_file.unlink(missing_ok=True)
             return False

        logger.debug(f"创建了拼接列表文件: {concat_list_file.name}")

        # FFmpeg 命令：使用 concat demuxer
        # -f concat 指定输入格式
        # -safe 0 允许非相对路径
        # -i 输入列表文件
        # -c copy 直接复制代码流（包括视频和音频），速度快
        # -y 覆盖输出文件
        cmd_list = [
            ffmpeg_path, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_file.resolve()),
            "-c", "copy",
            str(output_path.resolve())
        ]
        logger.debug(f"  执行 FFmpeg 命令: {shlex.join(cmd_list)}")
        result = subprocess.run(cmd_list, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')

        # 检查返回码
        if result.returncode != 0:
            logger.error(f"FFmpeg 拼接视频失败。返回码: {result.returncode}")
            logger.error(f"FFmpeg 命令: {shlex.join(cmd_list)}")
            if result.stdout: logger.error(f"FFmpeg STDOUT:\n{result.stdout}")
            if result.stderr: logger.error(f"FFmpeg STDERR:\n{result.stderr}")
            if output_path.exists(): output_path.unlink(missing_ok=True) # 清理可能产生的输出文件
            return False

        logger.info(f"视频拼接成功: {output_path.name}")
        return True

    except FileNotFoundError:
         logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
         return False
    except Exception as e:
         logger.error(f"创建拼接列表或执行拼接时发生错误: {e}", exc_info=True)
         if output_path.exists(): output_path.unlink(missing_ok=True)
         return False
    finally:
         # 清理临时列表文件
         if concat_list_file.exists():
             try: concat_list_file.unlink()
             except OSError: pass


def add_subtitles(input_video: Path, srt_file: Path, output_video: Path, logger: logging.Logger, config: configparser.ConfigParser) -> bool:
    """
    使用 FFmpeg 将 SRT 字幕硬编码到视频中。
    失败时返回 False。

    Args:
        input_video: 输入视频文件的 Path 对象。
        srt_file: SRT 字幕文件的 Path 对象。
        output_video: 输出视频文件的 Path 对象。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        bool: 字幕添加成功返回 True，否则返回 False。
    """
    logger.info(f"使用 FFmpeg 添加字幕到视频 '{input_video.name}'...")

    ffmpeg_path = get_tool_path("ffmpeg", logger, config)
    if ffmpeg_path is None:
         logger.error("FFmpeg 路径未解析，无法添加字幕。")
         return False
    if not input_video.is_file():
         logger.error(f"输入视频文件不存在: {input_video}")
         return False
    if not srt_file.is_file():
         logger.error(f"字幕文件不存在: {srt_file}")
         return False

    # --- 获取字幕样式配置 ---
    # 从 config.ini 的 [Video] section 读取 'subtitle_style_ffmpeg' key
    # 使用一个合理的默认值作为回退
    ffmpeg_style_str = config.get(
        'Video',
        'subtitle_style_ffmpeg',
        fallback="Fontname=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,BackColour=&H9A000000,BorderStyle=1,Outline=1,Shadow=0.8,Alignment=2,MarginV=25"
    )
    logger.debug(f"使用的字幕样式 (force_style): {ffmpeg_style_str}")

    # --- 准备 FFmpeg filtergraph ---
    # 正确转义 SRT 文件路径给 FFmpeg filter
    # FFmpeg filtergraph 路径需要特殊转义
    srt_path_str = str(srt_file.resolve())
    # 示例转义：将 \ 替换为 /，将 : 替换为 \:，将 ' 替换为 \' 等
    # 一个更 robust 的方法是使用 FFmpeg 的 "file,..." 语法或创建 ASS 文件
    # 但对于常见路径，基本转义可能够用
    if platform.system() == "Windows":
         # Windows 路径转义 for filtergraph
         srt_path_escaped_for_filter = srt_path_str.replace('\\', '/').replace(':', r'\:')
         # 需要双重转义反斜杠 'C:\path\to\sub.srt' -> 'C\\:\\path\\to\\sub.srt'
         srt_path_escaped_for_filter = srt_path_escaped_for_filter.replace('\\', '\\\\')
         # 还需要转义单引号
         srt_path_escaped_for_filter = srt_path_escaped_for_filter.replace("'", "'\\''") # 这通常在整体引用时用
         # 尝试一种常见写法
         filter_srt_path = srt_path_str.replace('\\', '/') # 先转换斜杠
         # 如果路径中有单引号，可能需要更复杂的转义或 file= 选项
         # filtergraph 参数整体可以用单引号或双引号包裹
    else:
         # macOS/Linux 路径转义 for filtergraph
         # 主要转义单引号
         filter_srt_path = srt_path_str.replace("'", "'\\''")


    # 构建 filtergraph，应用 force_style 和转义后的 SRT 路径
    # filtergraph = f"subtitles='{filter_srt_path}':force_style='{ffmpeg_style_str}'" # 使用单引号包裹 srt_path_escaped_for_filter
    # 尝试使用 file= 选项，可能对特殊字符更友好
    filtergraph = f"subtitles=file='{filter_srt_path}':force_style='{ffmpeg_style_str}'"


    input_video_str = str(input_video.resolve())
    output_video_str = str(output_video.resolve())

    # --- 构建 FFmpeg 命令 ---
    # -i 输入视频
    # -vf 视频滤镜 (包括字幕)
    # -c:v libx264 视频编码器
    # -preset medium 平衡速度和质量
    # -crf 22 质量因子 (更小=更高质量，文件更大)
    # -c:a copy 直接复制音频流
    # -y 覆盖输出
    cmd_list = [
        ffmpeg_path, "-y",
        "-i", input_video_str,
        "-vf", filtergraph, # 使用构建好的 filtergraph
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "22",
        "-c:a", "copy",
        output_video_str
    ]
    try:
        logger.debug(f"  执行 FFmpeg 命令 (添加字幕): {shlex.join(cmd_list)}")
        result = subprocess.run(cmd_list, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')

        if result.returncode != 0:
            logger.error(f"FFmpeg 添加字幕失败。返回码: {result.returncode}")
            logger.error(f"FFmpeg 命令: {shlex.join(cmd_list)}")
            if result.stdout: logger.error(f"FFmpeg STDOUT:\n{result.stdout}")
            if result.stderr: logger.error(f"FFmpeg STDERR:\n{result.stderr}")
            if output_video.exists(): output_video.unlink(missing_ok=True) # 清理可能产生的输出文件
            return False

        logger.info(f"字幕添加成功: {output_video.name}")
        return True
    except FileNotFoundError:
         logger.error(f"错误：找不到 FFmpeg 命令 '{ffmpeg_path}'。")
         return False
    except Exception as e:
         logger.error(f"添加字幕时发生未知错误: {e}", exc_info=True)
         if output_video.exists(): output_video.unlink(missing_ok=True)
         return False


# --- 视频合成主函数 (由 Celery 任务调用) ---
def synthesize_video_for_task(
    processed_data: list[dict],
    temp_run_dir: Path, # 任务的临时目录
    output_video_path: Path, # 最终视频输出路径
    logger: logging.Logger, # 日志记录器实例
    config: configparser.ConfigParser # 配置对象
) -> bool:
    """
    根据处理好的数据，使用 FFmpeg 合成最终视频 (无转场)。
    包括：生成视频片段、拼接、生成字幕、添加字幕。
    失败时应抛出异常或返回 False (取决于 Celery 任务如何处理返回值)。
    这里设计为返回 bool，失败时的详细日志已在内部记录。

    Args:
        processed_data: 字典列表，包含幻灯片信息 (图片路径、备注、音频路径、时长)。
        temp_run_dir: 任务的临时工作目录路径。
        output_video_path: 最终视频输出路径。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        bool: 成功返回 True，失败返回 False。
    """
    logger.info("--- 开始基于 FFmpeg 的视频合成流程 ---")
    if not processed_data:
        logger.error("输入数据为空，无法合成视频。")
        return False

    # 确保最终输出目录存在
    try:
        output_video_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"无法创建最终输出目录 {output_video_path.parent}: {e}")
        return False

    temp_segments_dir = temp_run_dir / "video_segments"
    try:
        temp_segments_dir.mkdir(exist_ok=True)
    except OSError as e:
         logger.error(f"无法创建视频片段临时目录 {temp_segments_dir}: {e}")
         return False

    segment_files = [] # 存储生成的视频片段文件路径 (Path 对象)

    # --- 1. 生成各幻灯片的视频片段 ---
    logger.info("步骤 1/3: 使用 FFmpeg 生成各幻灯片的视频片段")
    default_slide_duration = config.getfloat('Video', 'default_slide_duration', fallback=3.0)

    for i, data in enumerate(processed_data):
        slide_num = data.get('slide_number', i + 1)
        image_path_str = data.get('image_path')
        audio_path_str = data.get('audio_path')
        audio_duration = data.get('audio_duration', 0.0) # 确保获取到时长，默认为 0.0

        if not image_path_str or not Path(image_path_str).is_file():
            logger.warning(f"幻灯片 {slide_num}: 图片路径无效或文件不存在 '{image_path_str}'。跳过此片段。")
            continue # 跳过没有有效图片的幻灯片

        image_path = Path(image_path_str)
        audio_path = Path(audio_path_str) if audio_path_str and Path(audio_path_str).is_file() else None

        # --- 确定片段展示时长 ---
        clip_duration = 0.0
        if audio_duration is not None and audio_duration > 0.01: # 优先使用有效的音频时长
            clip_duration = audio_duration
            logger.debug(f"幻灯片 {slide_num}: 使用音频时长 {clip_duration:.3f}s")
        else:
            # 如果音频时长无效 (None, 0, 或太小)，使用默认时长
            clip_duration = default_slide_duration
            if audio_path:
                logger.warning(f"幻灯片 {slide_num}: 音频时长无效或过短({audio_duration if audio_duration is not None else 'None'}), 使用默认展示时长 {clip_duration}s")
            else:
                logger.info(f"幻灯片 {slide_num}: 无音频，使用默认展示时长 {clip_duration}s")
        # --- ----------------- ---

        segment_output_path = temp_segments_dir / f"segment_{slide_num}.mp4"

        # 调用 create_video_segment 函数
        success = create_video_segment(
            image_path,
            clip_duration, # 传递最终确定的时长
            audio_path if audio_duration is not None and audio_duration > 0.01 else None, # 只有音频时长有效才传递音频路径
            segment_output_path,
            logger, # 传递 logger
            config # 传递 config
        )
        if success:
            segment_files.append(segment_output_path)
        else:
            logger.error(f"未能创建幻灯片 {slide_num} 的视频片段。")
            # 这里可以选择直接返回失败，或者继续处理其他片段并最后报告问题
            # 为了简化，如果任何片段创建失败，整个合成任务就失败
            return False

    if not segment_files:
        logger.error("未能成功生成任何视频片段。")
        return False

    # --- 2. 拼接视频片段 ---
    logger.info("步骤 2/3: 使用 FFmpeg 拼接视频片段")
    base_video_path = temp_run_dir / "base_video_no_subs.mp4" # 拼接后的无字幕视频
    success_concat = concatenate_videos(segment_files, base_video_path, logger, config)
    if not success_concat:
        logger.error("拼接视频片段失败。")
        return False # 拼接失败，返回 False

    # --- 3. 生成字幕 ---
    logger.info("步骤 3/3: 生成字幕文件 (ASR) 并添加到视频")
    # 收集所有有效音频的路径，用于 ASR
    audio_paths_for_asr = [d.get('audio_path') for d in processed_data if d.get('audio_path') and d.get('audio_duration', 0) > 0.01]
    subtitle_file_path = temp_run_dir / "subtitles.srt"
    subtitles_generated = False
    asr_errors_occurred = False

    if audio_paths_for_asr: # 只有存在有效音频时才尝试生成字幕
        logger.info(f"发现 {len(audio_paths_for_asr)} 个有效音频片段用于 ASR。")
        try:
             subtitles_generated = generate_subtitles(
                audio_paths_for_asr,
                subtitle_file_path,
                temp_run_dir, # 传递临时目录
                logger, # 传递 logger
                config # 传递 config
             )
        except Exception as asr_e:
             logger.error(f"调用 generate_subtitles 时发生错误: {asr_e}", exc_info=True)
             asr_errors_occurred = True # 标记 ASR 过程出错
             subtitles_generated = False # 确保标记为未生成

    else:
        logger.info("没有有效时长的音频文件，跳过字幕生成。")


    # --- 添加字幕 (如果成功生成) ---
    srt_is_valid = subtitles_generated and subtitle_file_path.exists() and subtitle_file_path.stat().st_size > 5
    if srt_is_valid:
        logger.info("字幕文件有效，尝试添加字幕到视频。")
        # 创建一个临时输出文件，避免直接覆盖 base_video_path
        final_video_with_subs_path = temp_run_dir / "final_video_with_subs.mp4"
        success_sub = add_subtitles(base_video_path, subtitle_file_path, final_video_with_subs_path, logger, config)

        if success_sub:
            logger.info("字幕添加成功。将带有字幕的视频作为最终输出。")
            try:
                 # 将带有字幕的视频移动到最终输出位置
                 shutil.move(str(final_video_with_subs_path), str(output_video_path))
                 logger.info(f"最终视频 (带字幕) 已保存到: {output_video_path.resolve()}")
                 # 清理中间文件
                 if base_video_path.exists(): base_video_path.unlink(missing_ok=True)
                 if subtitle_file_path.exists(): subtitle_file_path.unlink(missing_ok=True)
                 return True # 整个合成流程成功

            except Exception as e:
                 logger.error(f"移动最终带字幕视频时出错: {e}", exc_info=True)
                 # 即使移动失败，也不要删除原始合成的视频，方便调试
                 # if final_video_with_subs_path.exists(): final_video_with_subs_path.unlink()
                 return False # 移动失败，任务失败
        else:
            logger.error("添加字幕失败。将输出不带字幕的视频。")
            # 添加字幕失败，将基础无字幕视频作为最终输出
            try:
                 shutil.move(str(base_video_path), str(output_video_path))
                 logger.warning(f"最终视频 (无字幕 - 因添加失败) 已保存到: {output_video_path.resolve()}")
                 # 清理可能产生的带字幕的临时文件
                 if final_video_with_subs_path.exists(): final_video_with_subs_path.unlink(missing_ok=True)
                 if subtitle_file_path.exists(): subtitle_file_path.unlink(missing_ok=True)
                 return True # 合成流程成功 (但不含字幕)
            except Exception as e:
                 logger.error(f"移动最终无字幕视频时出错: {e}", exc_info=True)
                 return False # 移动失败，任务失败

    else:
        # 如果 SRT 文件无效或生成失败
        logger.warning("跳过添加字幕 (字幕文件无效或生成失败)。将输出不带字幕的视频。")
        if asr_errors_occurred:
            logger.error("字幕生成过程中发生了错误，请检查日志。")

        # 将基础无字幕视频移动到最终输出位置
        try:
             shutil.move(str(base_video_path), str(output_video_path))
             logger.info(f"最终视频 (无字幕) 已保存到: {output_video_path.resolve()}")
             # 清理可能残留的 SRT 文件
             if subtitle_file_path.exists(): subtitle_file_path.unlink(missing_ok=True)
             return True # 合成流程成功 (但不含字幕)
        except Exception as e:
             logger.error(f"移动最终无字幕视频时出错: {e}", exc_info=True)
             return False # 移动失败，任务失败