"""知识库服务层（`BE-06`）目录。

服务拆分：
* `splitter.py` —— 知识切分（契约 `2.4.3` 的四种切分单元）
* `importer.py` —— 材料导入（CSV / Markdown → chunk）
* `service.py` —— chunk CRUD + 版本 + 导入编排
"""
