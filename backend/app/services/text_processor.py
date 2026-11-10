"""
Textatmanageserveservice
"""

import json
from pathlib import Path
from typing import List
from ..utils.file_parser import FileParser, split_text_into_chunks


class TextProcessor:
    """Text processor"""
    
    @staticmethod
    def extract_from_files(file_paths: List[str]) -> str:
        """Extract text from multiple files"""
        return FileParser.extract_from_multiple(file_paths)
    
    @staticmethod
    def split_text(
        text: str,
        chunk_size: int = 500,
        overlap: int = 50
    ) -> List[str]:
        """
        Split text
        
        Args:
            text: Original text
            chunk_size: Chunk size
            overlap: Overlap size
            
        Returns:
            Text chunk list
        """
        return split_text_into_chunks(text, chunk_size, overlap)

    @staticmethod
    def split_table(source: str, headers: List[str], records: List[dict], chunk_size: int) -> List[str]:
        """Pack complete records while repeating their source and column context."""
        display_source = source if len(source) <= 32 else source[:29] + '...'
        source_line = f"Source: {display_source}\n"
        prefix = f"{source_line}Columns: {', '.join(headers)}\n"
        chunks = []
        current = prefix
        for number, record in enumerate(records, 1):
            fields = ', '.join(f"{key}: {json.dumps(record.get(key), ensure_ascii=False)}" for key in headers)
            line = f"Record {number}: {fields}"
            if len(prefix) + len(line) <= chunk_size:
                if len(current) + len(line) + 1 > chunk_size and current != prefix:
                    chunks.append(current.rstrip())
                    current = prefix
                current += line + '\n'
                continue
            if current != prefix:
                chunks.append(current.rstrip())
                current = prefix
            # Repeat a short identifying field on every part of an oversized row.
            identity_key = next((key for key in headers if record.get(key) not in (None, '') and len(json.dumps(record[key], ensure_ascii=False)) <= 40), None)
            identity = f"{identity_key}: {json.dumps(record.get(identity_key), ensure_ascii=False)}" if identity_key else ""
            for key in headers:
                value = json.dumps(record.get(key), ensure_ascii=False)
                offset = 0
                part = 1
                while offset < len(value):
                    context = identity if identity_key != key else ""
                    columns = f"{identity_key}, {key}" if context else key
                    field_prefix = f"{source_line}Columns: {columns}\n"
                    label = f"Record {number} [{context}], {key} (part {part}): " if context else f"Record {number}, {key} (part {part}): "
                    capacity = chunk_size - len(field_prefix) - len(label)
                    if capacity <= 0 and context:
                        field_prefix = f"{source_line}Columns: {key}\n"
                        label = f"Record {number} (part {part}): "
                        capacity = chunk_size - len(field_prefix) - len(label)
                    if capacity <= 0:
                        # Wide field names need compact labels, but keep source and the full name.
                        field_prefix = f"S:{display_source[:8]}\nColumns:{key}\n"
                        label = f"R{number}.{part}: "
                        capacity = chunk_size - len(field_prefix) - len(label)
                    if capacity <= 0:
                        raise ValueError(f"Table field name in {source} exceeds chunk size")
                    chunks.append(field_prefix + label + value[offset:offset + capacity])
                    offset += capacity
                    part += 1
        if current != prefix:
            chunks.append(current.rstrip())
        return chunks

    @staticmethod
    def split_project_files(project_id: str, files: List[dict], chunk_size: int, overlap: int) -> List[str]:
        """Split mixed uploads by format, retaining table records and headers."""
        from ..models.project import ProjectManager

        files_dir = Path(ProjectManager._get_project_files_dir(project_id))
        chunks = []
        for file in files:
            saved_name = file.get('saved_filename')
            if not saved_name:
                raise ValueError("Missing saved filename for table-aware project")
            path = files_dir / saved_name
            source = file['filename']
            if path.suffix.lower() in {'.csv', '.json'}:
                headers, records = FileParser.parse_table(str(path))
                chunks.extend(TextProcessor.split_table(source, headers, records, chunk_size))
            else:
                document = TextProcessor.preprocess_text(FileParser.extract_text(str(path)))
                chunks.extend(TextProcessor.split_text(f"=== {source} ===\n{document}", chunk_size, overlap))
        return chunks
    
    @staticmethod
    def preprocess_text(text: str) -> str:
        """
        preatmanageText
        - moveexceptmanyremainingspacewhite
        - markprepareconvertswitchperform
        
        Args:
            text: sourcestartText
            
        Returns:
            atmanageaftersText
        """
        import re
        
        # Normalize line breaks
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        
        # Remove consecutive blank lines (keep at most two line breaks)
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        # Remove leading/trailing whitespace
        lines = [line.strip() for line in text.split('\n')]
        text = '\n'.join(lines)
        
        return text.strip()
    
    @staticmethod
    def get_text_stats(text: str) -> dict:
        """Get text statistics"""
        return {
            "total_chars": len(text),
            "total_lines": text.count('\n') + 1,
            "total_words": len(text.split()),
        }

