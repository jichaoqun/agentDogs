# Tools
功能：根据Agent的任务调度，调用Tools完成任务
所有能力均采用插件化设计。
统一注册。
统一调用。
统一权限管理。

## 文件工具
支持：
read_file
write_file
move_file
copy_file
delete_file
rename_file
search_file

## 搜索工具
支持：
web_search
local_search
keyword_search

## 文档工具
支持：
PDF解析
Word解析
Excel解析
Markdown解析
OCR解析

## 代码工具
支持：
Python执行
Shell执行
Notebook执行
项目扫描

## AI工具
支持：
文本总结
信息抽取
分类
翻译
向量化

## 执行沙箱（Sandbox/Safe Execution）： 
你的需求提到了“代码能力”和“文件操作”。这具有极大的安全隐患。 如果LLM理解错误，执行了删除系统文件或死循环代码怎么办？本地助手必须有一个隔离的沙箱环境（如 Docker、WASM，或者严格的本地目录权限限制），限制其只能操作指定的“工作区（Workspace）”文件夹。