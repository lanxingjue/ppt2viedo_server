# core_logic/ppt_exporter_libreoffice.py
import os
import platform
import subprocess
import logging
import shutil
from pathlib import Path
import tempfile
import configparser
import shlex # 确保导入

# 导入共享工具函数
from .utils import get_tool_path, get_poppler_path

# pdf2image 的导入和可用性检查
try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    logging.error("缺少 'pdf2image' 库。请运行 'pip install pdf2image'。")
    PDF2IMAGE_AVAILABLE = False

def export_slides_with_libreoffice(
    pptx_filepath: Path,
    output_dir: Path,
    logger: logging.Logger, # 接收 logger
    config: configparser.ConfigParser # 接收 config
) -> list[str] | None:
    """
    使用 LibreOffice 将 PPTX 转换为 PDF，然后使用 pdf2image 将 PDF 转换为 PNG 图片。
    失败时返回 None。

    Args:
        pptx_filepath: 输入的 PPTX 文件的 Path 对象。
        output_dir: 保存导出 PNG 图片的目标目录的 Path 对象。
        logger: 日志记录器实例。
        config: ConfigParser 对象。

    Returns:
        一个包含所有成功导出的图片文件绝对路径的列表 (list[str])。
        如果发生错误，则返回 None。
    """
    logger.info(f"开始使用 LibreOffice 导出: '{pptx_filepath.name}' 到 '{output_dir}'")

    if not PDF2IMAGE_AVAILABLE:
        logger.error("pdf2image 库不可用，无法进行 PDF 到图片的转换。")
        return None

    # 使用工具函数获取 LibreOffice 路径
    libreoffice_path = get_tool_path("soffice", logger, config)
    if libreoffice_path is None:
        logger.error("LibreOffice (soffice) 未找到。请检查安装和配置。")
        return None

    # 使用工具函数获取 Poppler 路径
    poppler_path_for_pdf2image = get_poppler_path(logger, config)

    # 1. 检查输入文件
    if not pptx_filepath.is_file():
        logger.error(f"输入文件不存在: {pptx_filepath}")
        return None

    # 2. 确保输出目录存在
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"确保输出目录存在: {output_dir}")
    except OSError as e:
        logger.error(f"创建或访问输出目录失败: {output_dir} - {e}")
        return None

    # 3. 创建临时目录存放中间 PDF 文件
    try:
        # 使用 with 语句确保临时目录会被清理
        with tempfile.TemporaryDirectory(prefix="lo_pdf_") as temp_pdf_dir_str:
            temp_pdf_dir = Path(temp_pdf_dir_str)
            pdf_output_path = temp_pdf_dir / f"{pptx_filepath.stem}.pdf"
            logger.info(f"创建临时 PDF 目录: {temp_pdf_dir}")

            # 4. 调用 LibreOffice 将 PPTX 转换为 PDF
            cmd_convert_to_pdf = [
                libreoffice_path,
                "--headless",           # 无头模式
                "--invisible",          # 尝试添加 invisible 标志，有时有帮助
                "--nologo",             # 不显示 logo
                "--nolockcheck",        # 禁用文件锁定检查
                "--norestore",          # 禁用恢复功能
                "--convert-to", "pdf:writer_pdf_Export", # 更明确的 PDF 导出 filter
                "--outdir", str(temp_pdf_dir.resolve()),
                str(pptx_filepath.resolve())
            ]
            logger.info(f"执行 LibreOffice 命令: {shlex.join(cmd_convert_to_pdf)}") # 使用 shlex.join 引用参数
            try:
                # 增加超时时间，PPT 转换可能较慢
                timeout_seconds = config.getint('General', 'libreoffice_timeout', fallback=180)
                logger.debug(f"LibreOffice 超时设置为: {timeout_seconds} 秒")
                result_pdf = subprocess.run(
                    cmd_convert_to_pdf,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=True, # 让它在返回码非 0 时抛出异常
                    encoding='utf-8',
                    errors='ignore'
                )
                logger.info("LibreOffice 转换 PDF 命令执行完成。")
                if result_pdf.stdout: logger.debug(f"LibreOffice STDOUT:\n{result_pdf.stdout}")
                if result_pdf.stderr: logger.debug(f"LibreOffice STDERR:\n{result_pdf.stderr}")

                if not pdf_output_path.exists() or pdf_output_path.stat().st_size == 0:
                    logger.error(f"LibreOffice 命令执行后未找到或生成了空的 PDF 文件: {pdf_output_path}")
                    try:
                        files_in_temp = list(temp_pdf_dir.glob('*'))
                        logger.error(f"临时 PDF 目录内容: {files_in_temp}")
                    except Exception as list_e: logger.error(f"无法列出临时 PDF 目录内容: {list_e}")
                    return None

            except subprocess.CalledProcessError as e:
                logger.error(f"LibreOffice 转换 PDF 失败。返回码: {e.returncode}")
                logger.error(f"命令: {shlex.join(cmd_convert_to_pdf)}")
                logger.error(f"STDERR:\n{e.stderr}")
                logger.error(f"STDOUT:\n{e.stdout}")
                return None
            except subprocess.TimeoutExpired:
                logger.error(f"LibreOffice 转换 PDF 超时 ({timeout_seconds} 秒)。PPT 文件可能过大或复杂。")
                return None
            except FileNotFoundError:
                logger.error(f"错误：找不到 LibreOffice 命令 '{libreoffice_path}'。")
                return None
            except Exception as e:
                logger.error(f"执行 LibreOffice 转换时发生未知错误: {e}", exc_info=True)
                return None

            # 5. 调用 pdf2image 将 PDF 转换为图片
            logger.info("开始使用 pdf2image 将 PDF 转换为 PNG 图片...")
            exported_files = []
            try:
                # dpi 控制输出图片的分辨率，可以从配置读取
                dpi = config.getint('Video', 'image_export_dpi', fallback=150)
                logger.debug(f"使用 DPI: {dpi} 进行 PDF 到图片转换")

                images = convert_from_path(
                    pdf_output_path,
                    output_folder=output_dir,
                    fmt='png',
                    output_file="slide_", # PyInstaller 打包后可能需要绝对路径或特殊处理
                    paths_only=True,
                    dpi=dpi,
                    use_pdftocairo=True, # 优先使用 pdftocairo
                    poppler_path=poppler_path_for_pdf2image, # 传递获取到的 Poppler 路径
                    # 增加线程数可能加速，但需测试稳定性
                    # thread_count=max(1, os.cpu_count() // 2)
                )

                # --- 修复文件名和排序 ---
                # pdf2image 返回的路径可能不是按顺序的，文件名也可能包含 uuid
                # 我们需要找到所有生成的图片，按修改时间排序（近似创建顺序），然后重命名
                generated_images = sorted(
                    output_dir.glob("slide_*.png"),
                    key=lambda p: p.stat().st_mtime # 按修改时间排序
                )
                num_pages = len(generated_images)
                logger.info(f"pdf2image 成功转换了 {num_pages} 页。")

                for i, old_path in enumerate(generated_images):
                    slide_number = i + 1
                    new_filename = f"slide_{slide_number}.png"
                    new_path = output_dir / new_filename
                    try:
                        # 如果目标文件已存在且不是同一个文件，删除
                        if new_path.exists() and old_path.resolve() != new_path.resolve():
                            logger.warning(f"目标文件 {new_path.name} 已存在，将被覆盖。")
                            new_path.unlink()
                        # 如果文件名已经是正确的，则不需要重命名
                        if old_path.name != new_filename:
                            old_path.rename(new_path)
                            logger.debug(f"  重命名图片: {old_path.name} -> {new_path.name}")
                        else:
                            logger.debug(f"  图片文件名已正确: {new_path.name}")
                        exported_files.append(str(new_path.resolve()))
                    except OSError as rename_e:
                        logger.error(f"  重命名图片 {old_path.name} 到 {new_path.name} 失败: {rename_e}")
                        # 即使重命名失败，也尝试添加原始路径，后续步骤可能会出错
                        exported_files.append(str(old_path.resolve()))


                if len(exported_files) != num_pages:
                    logger.warning(f"整理后的图片数量 ({len(exported_files)}) 与转换的页面数量 ({num_pages}) 不符。")

                if not exported_files:
                    logger.error("未能成功整理任何导出的图片。")
                    return None

                logger.info(f"成功导出并整理了 {len(exported_files)} 张图片。")
                return exported_files

            except Exception as e:
                # 提供更具体的错误信息
                error_str = str(e).lower()
                if "unable to get page count" in error_str or "pdfinfo" in error_str:
                     logger.error(f"pdf2image 错误: 无法获取 PDF 信息。请确保 Poppler 工具已安装并可在 PATH 或配置路径 '{poppler_path_for_pdf2image}' 中找到。", exc_info=True)
                elif "pdftocairo" in error_str or "pdftoppm" in error_str:
                     logger.error(f"pdf2image 错误: Poppler 工具 (pdftocairo/pdftoppm) 执行失败。请确保 Poppler 安装完整且路径配置正确。", exc_info=True)
                else:
                     logger.error(f"pdf2image 转换 PDF 到图片时出错: {e}", exc_info=True)
                return None

    except Exception as outer_e:
        # 捕获 with tempfile.TemporaryDirectory 可能的错误
        logger.error(f"处理临时目录或导出过程中发生意外错误: {outer_e}", exc_info=True)
        return None

    # 临时 PDF 目录会在 with 语句结束时自动清理