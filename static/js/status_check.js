// static/js/status_check.js

document.addEventListener('DOMContentLoaded', function() {
    // 获取页面元素
    const taskId = document.getElementById('task-id').textContent.trim(); // 任务ID
    const statusText = document.getElementById('task-status'); // 任务状态文本元素 (如 "PENDING", "STARTED")
    const statusStage = document.getElementById('status-stage'); // 任务阶段文本元素

    // 获取进度条相关元素
    const taskProgressContainer = document.getElementById('progress-container'); // 进度条容器
    const taskProgressBarFill = document.getElementById('task-progress'); // 进度条填充元素
    const progressPercentage = document.getElementById('progress-percentage'); // 进度百分比文本

    const downloadLinkDiv = document.getElementById('download-link'); // 下载链接区域
    const videoDownloadUrl = document.getElementById('video-download-url'); // 下载链接 A 标签
    const completionTime = document.getElementById('completion-time'); // 完成时间文本

    const errorMessageDiv = document.getElementById('error-message'); // 错误信息区域
    const errorSummary = document.getElementById('error-summary'); // 错误概要文本元素
    const errorDetails = document.getElementById('error-details'); // 详细错误/堆栈跟踪文本元素 (pre 标签)
    const showTracebackButton = document.getElementById('show-traceback'); // 显示详细信息按钮

    // 初始状态来自后端渲染
    updateStatusDisplay(statusText.textContent);

    // 隐藏详细错误部分和按钮，直到有错误发生
    errorDetails.style.display = 'none';
    showTracebackButton.style.display = 'none';

    // 初始隐藏下载链接区域
    downloadLinkDiv.style.display = 'none';
    // 初始隐藏进度条容器 (如果后端初始状态不是进行中)
    if (statusText.textContent !== 'STARTED' && statusText.textContent !== 'PROCESSING') {
        if(taskProgressContainer) taskProgressContainer.style.display = 'none';
    }


    // 绑定显示/隐藏详细错误按钮的事件
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
         if (state === 'PENDING') { color = '#ffff00'; text = '排队中'; }
         else if (state === 'STARTED' || state === 'PROCESSING') { color = '#00ffff'; text = '处理中'; }
         else if (state === 'SUCCESS') { color = '#00ff00'; text = '完成'; }
         else if (state === 'FAILURE') { color = '#ff0000'; text = '失败'; }
         else if (state === 'REVOKED') { color = '#ff0000'; text = '已取消'; }
         else if (state === 'RETRY') { color = '#ffa500'; text = '重试中'; }
         else if (state === 'ERROR') { color = '#ff0000'; text = '错误'; }
         else if (state === 'COMPLETE') { color = '#00ff00'; text = '完成'; } // 兼容 tasks.py 中可能返回的 COMPLETE 状态


         statusText.textContent = text;
         statusText.style.color = color;
         statusText.style.textShadow = `0 0 3px ${color}`;
    }


    // 轮询任务状态的函数
    function checkStatus() {
        const statusUrl = `/tasks/${taskId}/status`;

        fetch(statusUrl)
            .then(response => {
                 if (!response.ok) {
                     console.error(`HTTP error! status: ${response.status}`);
                     statusStage.textContent = `无法获取任务状态 (HTTP ${response.status})。`;
                     updateStatusDisplay('ERROR');
                     errorSummary.textContent = `网络或服务器错误，HTTP 状态码: ${response.status}`;
                     errorMessageDiv.style.display = 'block';
                     downloadLinkDiv.style.display = 'none';
                     showTracebackButton.style.display = 'none';
                     if(taskProgressContainer) taskProgressContainer.style.display = 'none'; // 错误时隐藏进度条
                     throw new Error(`HTTP status ${response.status}`);
                 }
                 return response.json();
             })
            .then(data => {
                updateStatusDisplay(data.state);

                if (data.meta && data.meta.stage) {
                     statusStage.textContent = `阶段: ${data.meta.stage}`;
                     if (data.meta.status) {
                         statusStage.textContent += ` (${data.meta.status})`;
                     }
                } else if (data.state === 'PENDING') {
                     statusStage.textContent = '阶段: 排队等待中...';
                } else {
                     statusStage.textContent = '阶段: 处理中...';
                }

                // 更新进度条和百分比文本
                if (taskProgressContainer && data.meta && typeof data.meta.progress === 'number') {
                    const progress = Math.max(0, Math.min(100, data.meta.progress));
                    taskProgressContainer.style.display = 'flex'; // 显示进度条容器
                    if (taskProgressBarFill) taskProgressBarFill.style.width = progress + '%';
                    if (progressPercentage) progressPercentage.textContent = Math.round(progress) + '%';
                } else if (taskProgressContainer && (data.state === 'SUCCESS' || data.state === 'COMPLETE')) {
                    // 如果任务成功，确保进度条满格
                    taskProgressContainer.style.display = 'flex';
                    if (taskProgressBarFill) taskProgressBarFill.style.width = '100%';
                    if (progressPercentage) progressPercentage.textContent = '100%';
                } else if (taskProgressContainer && data.state !== 'PENDING' && data.state !== 'STARTED' && data.state !== 'PROCESSING' && data.state !== 'RETRY') {
                    // 对于非进行中且无进度信息的最终状态（如早期失败），隐藏进度条
                    taskProgressContainer.style.display = 'none';
                }


                if (data.state === 'PENDING' || data.state === 'STARTED' || data.state === 'PROCESSING' || data.state === 'RETRY') {
                    setTimeout(checkStatus, 2000);
                } else if (data.state === 'SUCCESS' || data.state === 'COMPLETE') { // 兼容 COMPLETE 状态
                    statusStage.textContent = '阶段: 完成';
                    downloadLinkDiv.style.display = 'block';
                    errorMessageDiv.style.display = 'none';
                    showTracebackButton.style.display = 'none';
                    if(taskProgressContainer && taskProgressBarFill && progressPercentage) { // 确保成功时进度条100%
                        taskProgressBarFill.style.width = '100%';
                        progressPercentage.textContent = '100%';
                    }


                    if (data.download_url) {
                         videoDownloadUrl.href = data.download_url;
                         const filename = decodeURIComponent(data.download_url.split('/').pop().split('?')[0]);
                         videoDownloadUrl.textContent = `下载视频: ${filename}`;
                    } else if (data.result) {
                         // 尝试从 result 字段构造下载链接 (通常是相对路径或文件名)
                         const resultPath = data.result;
                         const filenameFromResult = resultPath.includes('/') ? resultPath.substring(resultPath.lastIndexOf('/') + 1) : resultPath;
                         videoDownloadUrl.href = `/output/${encodeURIComponent(filenameFromResult)}`;
                         videoDownloadUrl.textContent = `下载视频: ${filenameFromResult}`;
                         console.warn("API 未提供 download_url，尝试使用 result 字段构造下载链接。");
                    } else {
                         videoDownloadUrl.textContent = '下载链接不可用';
                         errorSummary.textContent = "任务状态为成功，但未返回有效的视频文件信息。请检查服务器日志。";
                         errorMessageDiv.style.display = 'block';
                         downloadLinkDiv.style.display = 'none';
                    }

                    if (data.meta && data.meta.duration !== undefined) {
                        completionTime.textContent = `总耗时: ${data.meta.duration.toFixed(2)} 秒`;
                    } else {
                         completionTime.textContent = `任务完成`;
                    }

                } else if (data.state === 'FAILURE' || data.state === 'REVOKED') {
                    statusStage.textContent = '阶段: 失败';
                    downloadLinkDiv.style.display = 'none';
                    errorMessageDiv.style.display = 'block';
                    errorDetails.style.display = 'none';
                    if(taskProgressContainer) taskProgressContainer.style.display = 'none'; // 失败时隐藏进度条

                    if (data.meta && data.meta.error) {
                        errorSummary.textContent = data.meta.error;
                        if (data.meta.traceback) {
                            errorDetails.textContent = data.meta.traceback;
                            showTracebackButton.style.display = 'inline-block';
                        } else {
                            showTracebackButton.style.display = 'none';
                        }
                    } else if (data.error) {
                        errorSummary.textContent = data.error;
                        showTracebackButton.style.display = 'none';
                    } else {
                        errorSummary.textContent = '服务器返回未知错误。请检查服务器日志获取详细信息。';
                        showTracebackButton.style.display = 'none';
                    }
                } else {
                     statusStage.textContent = `未知状态: ${data.state}`;
                     updateStatusDisplay(data.state);
                     setTimeout(checkStatus, 5000);
                }
            })
            .catch(error => {
                console.error('Error checking task status:', error);
                if (statusText.textContent !== 'ERROR' && statusText.textContent !== '失败' && statusText.textContent !== '已取消') {
                     statusStage.textContent = '网络错误，无法获取任务状态。';
                     updateStatusDisplay('ERROR');
                     errorSummary.textContent = '网络连接错误或服务器问题，请检查服务器状态并稍后刷新页面。';
                     errorMessageDiv.style.display = 'block';
                     downloadLinkDiv.style.display = 'none';
                     showTracebackButton.style.display = 'none';
                     if(taskProgressContainer) taskProgressContainer.style.display = 'none'; // 网络错误时隐藏进度条
                }
            });
        }

    const initialStatus = statusText.textContent;
    if (initialStatus !== '完成' && initialStatus !== '失败' && initialStatus !== '错误' && initialStatus !== '已取消' &&
        initialStatus.toUpperCase() !== 'SUCCESS' && initialStatus.toUpperCase() !== 'FAILURE' && initialStatus.toUpperCase() !== 'ERROR' && initialStatus.toUpperCase() !== 'REVOKED' && initialStatus.toUpperCase() !== 'COMPLETE') {
         setTimeout(checkStatus, 500);
    } else {
         checkStatus(); // 如果是最终状态，也调用一次以填充 meta
         if (initialStatus.toUpperCase() === 'SUCCESS' || initialStatus.toUpperCase() === 'COMPLETE' || initialStatus === '完成') {
            if(taskProgressContainer && taskProgressBarFill && progressPercentage) {
                taskProgressContainer.style.display = 'flex';
                taskProgressBarFill.style.width = '100%';
                progressPercentage.textContent = '100%';
            }
         } else {
            if(taskProgressContainer) taskProgressContainer.style.display = 'none';
         }
    }
});
