# core_logic/utils.py
import logging
import shutil
import platform
import configparser
from pathlib import Path
import subprocess
import shlex
import json
import wave
import contextlib
import os

# 注意：这个模块中的函数可能被不同的进程（Web服务器、Celery worker）调用，
# 因此配置和日志最好由调用者传入，或者有一个全局初始化的方式。
# 这里暂时假设调用者会传入 logger 和 config 对象。

def get_tool_path(tool_name: str, logger: logging.Logger, config: configparser.ConfigParser) -> str | None:
    """
    确定外部工具（如 ffmpeg, ffprobe, soffice）的可执行文件路径。
    优先从 config.ini 的 [Paths] 部分读取，然后尝试系统 PATH。

    Args:
        tool_name: 工具的名称 (例如 "ffmpeg", "soffice")。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        找到的工具的绝对路径字符串，如果找不到则返回 None。
    """
    # 优先从 config.ini 读取 tool_name 对应的路径
    tool_path_config = config.get('Paths', f'{tool_name}_path', fallback=tool_name)
    logger.debug(f"尝试查找工具 '{tool_name}'，配置路径/名称: '{tool_path_config}'")

    # 尝试在 PATH 或配置路径中查找
    tool_executable_found = shutil.which(tool_path_config)
    if tool_executable_found:
        resolved_path = str(Path(tool_executable_found).resolve())
        logger.info(f"通过 which 找到 '{tool_name}' 可执行文件: {resolved_path}")
        return resolved_path

    # 如果配置的不是默认名称且在 PATH 中找不到，尝试在 PATH 中查找默认名称
    if tool_path_config != tool_name:
        logger.debug(f"配置路径 '{tool_path_config}' 未找到，尝试在 PATH 中查找默认名称 '{tool_name}'")
        tool_executable_found = shutil.which(tool_name)
        if tool_executable_found:
            resolved_path = str(Path(tool_executable_found).resolve())
            logger.info(f"通过 which 找到默认名称 '{tool_name}' 可执行文件: {resolved_path}")
            return resolved_path

    # 针对 macOS 的特殊处理：检查 LibreOffice 的默认安装路径
    if tool_name == "soffice" and platform.system() == "Darwin":
        common_path = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if Path(common_path).exists():
            logger.info(f"在默认 macOS 路径找到 '{tool_name}': {common_path}")
            return common_path

    logger.error(f"未能找到 '{tool_name}' 可执行文件！请确保已安装，"
                 f"并将其添加到系统 PATH 环境变量，或在 config.ini 的 [Paths] 部分正确配置其路径。")
    return None

def get_poppler_path(logger: logging.Logger, config: configparser.ConfigParser) -> str | None:
    """
    获取 Poppler 的 bin 目录路径，供 pdf2image 使用。
    优先从 config.ini 读取，如果无效或未配置，则返回 None (让 pdf2image 依赖系统 PATH)。

    Args:
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        Poppler bin 目录的绝对路径字符串，如果未配置或配置无效则返回 None。
    """
    poppler_path_config = config.get('Paths', 'poppler_path', fallback=None)
    if poppler_path_config:
        poppler_bin_path = Path(poppler_path_config)
        # 检查路径是否存在并且是一个目录
        if poppler_bin_path.is_dir():
            # 进一步检查目录下是否包含关键工具 (可选但推荐)
            if (poppler_bin_path / 'pdftoppm').exists() or (poppler_bin_path / 'pdftoppm.exe').exists():
                resolved_path = str(poppler_bin_path.resolve())
                logger.info(f"使用 config.ini 中配置的 Poppler 路径: {resolved_path}")
                return resolved_path
            else:
                logger.warning(f"配置的 Poppler 路径 '{poppler_path_config}' 中未找到 pdftoppm 工具。将依赖系统 PATH。")
                return None
        else:
            logger.warning(f"配置的 Poppler 路径 '{poppler_path_config}' 不是一个有效的目录。将依赖系统 PATH。")
            return None
    else:
        logger.info("config.ini 中未配置 Poppler 路径，pdf2image 将依赖系统 PATH。")
        return None


def get_audio_duration(filepath: Path, logger: logging.Logger, config: configparser.ConfigParser) -> float | None:
    """
    使用 FFprobe 获取音频文件的准确时长 (秒)。

    Args:
        filepath: 音频文件的 Path 对象。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        音频时长 (float)，如果无法获取则返回 None。
    """
    if not filepath or not filepath.is_file():
        logger.warning(f"尝试获取时长失败，文件无效或不存在: {filepath}")
        return None

    # 获取 FFprobe 路径
    ffprobe_path = get_tool_path("ffprobe", logger, config)
    if ffprobe_path is None:
        logger.error("无法获取音频时长，因为找不到 ffprobe。")
        return None

    command = [
        ffprobe_path,
        "-v", "quiet",           # 静默模式
        "-print_format", "json", # 输出为 JSON
        "-show_format",          # 显示格式信息 (包含 duration)
        "-show_streams",         # 显示流信息 (有时 duration 在流里)
        str(filepath.resolve())
    ]

    try:
        logger.debug(f"执行 ffprobe 获取时长: {shlex.join(command)}")
        # 设置 Popen 的 stderr=subprocess.PIPE 以便捕获错误输出
        # 使用 text=True (别名 universal_newlines=True) 来处理文本模式
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore')
        stdout, stderr = process.communicate(timeout=15) # 设置超时

        if process.returncode != 0:
            logger.error(f"执行 ffprobe 失败 for {filepath.name}。返回码: {process.returncode}")
            logger.error(f"FFprobe 命令: {shlex.join(command)}")
            if stderr:
                logger.error(f"FFprobe 错误输出:\n{stderr}")
            return None

        metadata = json.loads(stdout)
        duration = None

        # 优先从 format -> duration 获取
        if 'format' in metadata and 'duration' in metadata['format']:
            try:
                duration = float(metadata['format']['duration'])
                logger.debug(f"从 format 获取 {filepath.name} 时长: {duration:.3f}s")
            except (ValueError, TypeError, KeyError):
                 logger.warning(f"无法从 format.duration 解析 {filepath.name} 的有效时长: {metadata.get('format', {}).get('duration')}")

        # 如果 format 中没有或无效，尝试从第一个音频流的 duration 获取
        if duration is None and 'streams' in metadata:
            for stream in metadata['streams']:
                if stream.get('codec_type') == 'audio' and 'duration' in stream:
                    try:
                        duration = float(stream['duration'])
                        logger.debug(f"从 audio stream 获取 {filepath.name} 时长: {duration:.3f}s")
                        break # 找到第一个就够了
                    except (ValueError, TypeError, KeyError):
                        logger.warning(f"无法从 stream.duration 解析 {filepath.name} 的有效时长: {stream.get('duration')}")

        if duration is not None and duration >= 0.01: # 检查有效性 (大于等于 0.01 秒)
             return duration
        elif duration is not None: # 时长为 0 或过小
            logger.warning(f"FFprobe 获取的时长过短或为零 ({duration:.3f}s) for {filepath.name}，视为无效。")
            return None # 返回 None 表示无效
        else:
            logger.error(f"FFprobe 未能从 {filepath.name} 的元数据中找到有效的时长信息。")
            logger.debug(f"FFprobe 元数据: {metadata}") # 记录详细元数据以供调试
            return None

    except subprocess.TimeoutExpired:
        logger.error(f"执行 ffprobe 获取 {filepath.name} 时长超时。")
        if process: process.kill() # 尝试杀死超时的进程
        return None
    except json.JSONDecodeError as e:
        logger.error(f"解析 ffprobe 的 JSON 输出失败 for {filepath.name}: {e}")
        logger.error(f"FFprobe 原始输出 (stdout):\n{stdout}")
        return None
    except FileNotFoundError:
        logger.error(f"错误：找不到 ffprobe 命令 '{ffprobe_path}'。")
        return None
    except Exception as e:
        logger.error(f"使用 ffprobe 获取 {filepath.name} 时长时发生未知错误: {e}", exc_info=True)
        return None

def get_wav_duration_fallback(filepath: Path, logger: logging.Logger) -> float:
    """
    (备用方法) 使用 wave 模块获取 WAV 文件时长，仅当 ffprobe 失败或不可用时考虑。
    注意：准确性可能不如 ffprobe。
    """
    if not filepath.is_file():
        logger.warning(f"[Fallback] 尝试获取 WAV 时长失败，文件不存在: {filepath}")
        return 0.0
    try:
        with contextlib.closing(wave.open(str(filepath), 'r')) as f:
            frames = f.getnframes()
            rate = f.getframerate()
            if rate <= 0:
                logger.warning(f"[Fallback] WAV 文件采样率无效或为零: {filepath}")
                return 0.0
            duration = frames / float(rate)
            logger.debug(f"[Fallback] 使用 wave 模块获取 {filepath.name} 时长: {duration:.3f}s")
            return duration if duration >= 0.01 else 0.0 # 同样过滤过短时长
    except wave.Error as e:
        logger.error(f"[Fallback] 读取 WAV 文件头出错 {filepath}: {e}")
        return 0.0
    except Exception as e:
        logger.error(f"[Fallback] 获取 WAV 时长时发生意外错误 {filepath}: {e}")
        return 0.0