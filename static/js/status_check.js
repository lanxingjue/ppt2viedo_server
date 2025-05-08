// static/js/status_check.js

document.addEventListener('DOMContentLoaded', function() {
    // 获取页面元素
    const taskId = document.getElementById('task-id').textContent.trim(); // 任务ID
    const statusText = document.getElementById('task-status'); // 任务状态文本元素 (如 "PENDING", "STARTED")
    const statusStage = document.getElementById('status-stage'); // 任务阶段文本元素

    // TODO: 如果启用了进度条，这里需要获取进度条和百分比元素
    // const taskProgressContainer = document.getElementById('progress-container'); // 进度条容器
    // const taskProgressBarFill = document.getElementById('task-progress'); // 进度条填充元素
    // const progressPercentage = document.getElementById('progress-percentage'); // 进度百分比文本

    const downloadLinkDiv = document.getElementById('download-link'); // 下载链接区域
    const videoDownloadUrl = document.getElementById('video-download-url'); // 下载链接 A 标签
    const completionTime = document.getElementById('completion-time'); // 完成时间文本

    const errorMessageDiv = document.getElementById('error-message'); // 错误信息区域
    const errorSummary = document.getElementById('error-summary'); // 错误概要文本元素
    const errorDetails = document.getElementById('error-details'); // 详细错误/堆栈跟踪文本元素 (pre 标签)
    const showTracebackButton = document.getElementById('show-traceback'); // 显示详细信息按钮

    // 初始状态来自后端渲染
    // statusText.textContent 包含了初始状态 (PENDING, STARTED, etc.)
    updateStatusDisplay(statusText.textContent);

    // 隐藏详细错误部分和按钮，直到有错误发生
    errorDetails.style.display = 'none';
    showTracebackButton.style.display = 'none';

    // 初始隐藏下载链接区域
    downloadLinkDiv.style.display = 'none';

    // 绑定显示/隐藏详细错误按钮的事件 (确保只绑定一次)
    if (showTracebackButton) {
         showTracebackButton.onclick = function() {
             if (errorDetails.style.display === 'none') {
                 errorDetails.style.display = 'block';
                 showTracebackButton.textContent = '隐藏详细信息';
             } else {
                 errorDetails.style.display = 'none';
                 showTracebackButton.textContent = '显示详细信息';
             }
         };
    }


    // 根据状态文本设置颜色和文本（更友好的中文状态）
    function updateStatusDisplay(state) {
         let color = '#cccccc'; // Default
         let text = state;
         if (state === 'PENDING') { color = '#ffff00'; text = '排队中'; } // 黄色/橙色
         else if (state === 'STARTED' || state === 'PROCESSING') { color = '#00ffff'; text = '处理中'; } // 青色
         else if (state === 'SUCCESS') { color = '#00ff00'; text = '完成'; } // 绿色
         else if (state === 'FAILURE') { color = '#ff0000'; text = '失败'; } // 红色
         else if (state === 'REVOKED') { color = '#ff0000'; text = '已取消'; } // 红色
         else if (state === 'RETRY') { color = '#ffa500'; text = '重试中'; } // 橙色
         else if (state === 'ERROR') { color = '#ff0000'; text = '错误'; } // 红色 (用于 HTTP 或客户端错误)


         statusText.textContent = text;
         statusText.style.color = color;
         statusText.style.textShadow = `0 0 3px ${color}`; // 添加发光效果
    }


    // 轮询任务状态的函数
    function checkStatus() {
        const statusUrl = `/tasks/${taskId}/status`;

        fetch(statusUrl)
            .then(response => {
                 if (!response.ok) {
                     console.error(`HTTP error! status: ${response.status}`);
                     // 如果 HTTP 错误，更新状态显示，不再轮询
                     statusStage.textContent = `无法获取任务状态 (HTTP ${response.status})。`;
                     updateStatusDisplay('ERROR');
                     errorSummary.textContent = `网络或服务器错误，HTTP 状态码: ${response.status}`;
                     errorMessageDiv.style.display = 'block';
                     downloadLinkDiv.style.display = 'none';
                     showTracebackButton.style.display = 'none';
                     // 抛出错误终止 promise 链，不再继续轮询
                     throw new Error(`HTTP status ${response.status}`);
                 }
                 return response.json();
             })
            .then(data => {
                // console.log("Task status data:", data); // 调试日志

                updateStatusDisplay(data.state); // 更新任务状态文本和颜色

                // --- 更新阶段和详细状态信息 ---
                if (data.meta && data.meta.stage) {
                     statusStage.textContent = `阶段: ${data.meta.stage}`;
                     // 显示 meta 中的额外状态信息（如当前处理项）
                     if (data.meta.status) {
                         statusStage.textContent += ` (${data.meta.status})`;
                     }

                } else if (data.state === 'PENDING') {
                     statusStage.textContent = '阶段: 排队等待中...';
                } else {
                     // 对于 STARTED/PROCESSING 但没有 meta.stage 的情况，显示通用信息
                     statusStage.textContent = '阶段: 处理中...';
                }

                // TODO: 如果任务发送进度百分比 (meta.progress)，在这里更新进度条和百分比文本
                // if (taskProgressContainer && data.meta && data.meta.progress !== undefined) {
                //     const progress = Math.max(0, Math.min(100, data.meta.progress));
                //     taskProgressContainer.style.display = 'flex'; // 显示进度条容器
                //     if (taskProgressBarFill) taskProgressBarFill.style.width = progress + '%';
                //     if (progressPercentage) progressPercentage.textContent = Math.round(progress) + '%';
                // } else if (taskProgressContainer) {
                //      taskProgressContainer.style.display = 'none'; // 没有进度信息则隐藏进度条
                // }


                // --- 根据任务状态执行不同操作 ---
                if (data.state === 'PENDING' || data.state === 'STARTED' || data.state === 'PROCESSING' || data.state === 'RETRY') {
                    // 任务仍在进行中，继续轮询
                    // 在这些状态下设置定时器进行下一次轮询
                    setTimeout(checkStatus, 2000); // 每 2 秒轮询一次

                } else if (data.state === 'SUCCESS') {
                    // 任务成功完成
                    statusStage.textContent = '阶段: 完成';
                    downloadLinkDiv.style.display = 'block'; // 显示下载链接区域
                    errorMessageDiv.style.display = 'none'; // 隐藏错误信息区域
                    showTracebackButton.style.display = 'none'; // 隐藏按钮

                    // 获取并设置下载链接
                    if (data.download_url) {
                         videoDownloadUrl.href = data.download_url;
                         const filename = decodeURIComponent(data.download_url.split('/').pop().split('?')[0]);
                         videoDownloadUrl.textContent = `下载视频: ${filename}`;
                    } else if (data.result) {
                         // 如果没有 download_url，尝试使用 result 字段构造
                         const assumedFilename = Path(data.result).name; // 假设 result 是路径字符串
                         const assumedDownloadUrl = `/output/${encodeURIComponent(assumedFilename)}`;
                         videoDownloadUrl.href = assumedDownloadUrl;
                         videoDownloadUrl.textContent = `下载视频: ${assumedFilename}`;
                         console.warn("API 未提供 download_url，尝试使用 result 字段构造下载链接。");
                    } else {
                         // 既没有 download_url 也没有 result
                         videoDownloadUrl.textContent = '下载链接不可用';
                         console.error("API 未返回 download_url 或 result。");
                         // 在这种情况下，虽然任务状态是 SUCCESS，但对用户来说是失败的
                         errorSummary.textContent = "任务状态为成功，但未返回有效的视频文件信息。请检查服务器日志。";
                         errorMessageDiv.style.display = 'block';
                         downloadLinkDiv.style.display = 'none';
                         // 保持 showTracebackButton 隐藏
                    }

                    // 显示总耗时（如果 meta 中有）
                    if (data.meta && data.meta.duration !== undefined) {
                        completionTime.textContent = `总耗时: ${data.meta.duration.toFixed(2)} 秒`;
                    } else {
                         completionTime.textContent = `任务完成`;
                    }

                    // 停止轮询 (任务已完成)

                } else if (data.state === 'FAILURE' || data.state === 'REVOKED') {
                    // 任务失败或被取消
                    statusStage.textContent = '阶段: 失败';
                    downloadLinkDiv.style.display = 'none'; // 隐藏下载链接
                    errorMessageDiv.style.display = 'block'; // 显示错误信息区域
                    errorDetails.style.display = 'none'; // 隐藏详细信息区域

                    // 显示错误信息 (从 meta 中获取)
                    if (data.meta && data.meta.error) {
                        errorSummary.textContent = data.meta.error; // 显示任务中记录的错误概要

                        // 显示详细堆栈跟踪（如果存在）
                        if (data.meta.traceback) {
                            errorDetails.textContent = data.meta.traceback;
                            showTracebackButton.style.display = 'inline-block'; // 显示按钮
                             // 绑定按钮点击事件已在 DOMContentLoaded 中完成

                        } else {
                            // 没有堆栈跟踪，隐藏按钮
                            showTracebackButton.style.display = 'none';
                        }

                    } else if (data.error) {
                        // 如果 meta 中没有详细错误信息，但顶级有 error 字段 (Celery 默认行为)
                        errorSummary.textContent = data.error;
                        showTracebackButton.style.display = 'none';
                    }
                     else {
                        // 如果没有错误信息，显示通用信息
                        errorSummary.textContent = '服务器返回未知错误。请检查服务器日志获取详细信息。';
                        showTracebackButton.style.display = 'none';
                    }

                    // 停止轮询 (任务已失败/取消)

                } else {
                     // 未知状态 - 可能还在处理中，或者进入了非标准状态
                     statusStage.textContent = `未知状态: ${data.state}`;
                     updateStatusDisplay(data.state);
                     // 继续轮询一段时间，或根据需要调整逻辑
                     setTimeout(checkStatus, 5000); // 间隔长一点

                }
            })
            .catch(error => {
                // 捕获 fetch 请求本身的网络错误等
                console.error('Error checking task status:', error);
                // 如果已经显示了错误信息，不再覆盖
                if (statusText.textContent !== 'ERROR' && statusText.textContent !== '失败' && statusText.textContent !== '已取消') {
                     statusStage.textContent = '网络错误，无法获取任务状态。';
                     updateStatusDisplay('ERROR');
                     errorSummary.textContent = '网络连接错误或服务器问题，请检查服务器状态并稍后刷新页面。';
                     errorMessageDiv.style.display = 'block';
                     downloadLinkDiv.style.display = 'none';
                     showTracebackButton.style.display = 'none';
                }
                // 停止轮询
            });
        }

    // 页面加载后，根据初始状态决定是否开始轮询
    const initialStatus = statusText.textContent;
    // 如果初始状态不是最终状态 (SUCCESS, FAILURE, REVOKED)，则开始轮询
    if (initialStatus !== 'SUCCESS' && initialStatus !== 'FAILURE' && initialStatus !== 'ERROR' && initialStatus !== 'REVOKED') {
         // 初始渲染时可能还没有 meta 信息，延迟检查确保 meta 信息已写入 backend
         setTimeout(checkStatus, 500);
    } else {
         // 如果初始状态是最终状态，手动触发一次 checkStatus 来填充 meta 信息和结果
         checkStatus();
    }


}); // DOMContentLoaded end