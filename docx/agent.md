# agent

## LLM 
支持主流的API调用，
支持ollama的调用，
支持本地的LLM的使用

## agent
主Agent + 专业Agent + Tools

### 主agent
功能：任务解析、任务规划、任务调度、信息补全
### 专业Agent
功能：根据主Agent的任务规划，调用专业Agent完成任务
1. File Agent
负责：
文件读取
文件写入
文件管理
文件搜索
文件预览

2. Knowledge Agent
负责：
知识库检索
RAG检索
GraphRAG检索
文档问答

3. Search Agent
负责：
网络搜索
网页解析
信息汇总

4. Code Agent
负责：
Python执行
Shell执行
项目分析
数据分析

5. Memory Agent
负责：
记忆写入
记忆检索
记忆压缩
记忆整理

6. Task Agent
负责：
长任务执行
工作流执行
状态跟踪
异常恢复

