"""
File Parser Utility
Supports text extraction from PDF, Markdown, TXT files
"""

import csv
import io
import json
from pathlib import Path
from typing import List, Optional


def _read_text_with_fallback(file_path: str) -> str:
    """
    Read text file with automatic encoding detection if UTF-8 fails.

    Uses multi-level fallback strategy:
    1. First try UTF-8 decoding
    2. Use charset_normalizer for encoding detection
    3. Fall back to chardet for encoding detection
    4. Finally use UTF-8 + errors='replace' as fallback

    Args:
        file_path: File path

    Returns:
        Decoded text content
    """
    data = Path(file_path).read_bytes()
    
    # First try UTF-8
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        pass

    # Try charset_normalizer for encoding detection
    encoding = None
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best and best.encoding:
            encoding = best.encoding
    except Exception:
        pass

    # Fall back to chardet
    if not encoding:
        try:
            import chardet
            result = chardet.detect(data)
            encoding = result.get('encoding') if result else None
        except Exception:
            pass

    # Final fallback: use UTF-8 + replace
    if not encoding:
        encoding = 'utf-8'

    return data.decode(encoding, errors='replace')


class FileParser:
    """File Parser"""

    SUPPORTED_EXTENSIONS = {'.pdf', '.md', '.markdown', '.txt', '.csv', '.json'}

    @classmethod
    def is_supported(cls, file_path: str) -> bool:
        """
        Check if the file format is supported

        Args:
            file_path: File path

        Returns:
            True if file format is supported, False otherwise
        """
        suffix = Path(file_path).suffix.lower()
        return suffix in cls.SUPPORTED_EXTENSIONS

    @classmethod
    def extract_text(cls, file_path: str) -> str:
        """
        Extract text from file

        Args:
            file_path: File path

        Returns:
            Extracted text content
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {file_path}")

        suffix = path.suffix.lower()

        if suffix not in cls.SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file format: {suffix}")

        if suffix == '.pdf':
            return cls._extract_from_pdf(file_path)
        elif suffix in {'.md', '.markdown'}:
            return cls._extract_from_md(file_path)
        elif suffix == '.txt':
            return cls._extract_from_txt(file_path)
        elif suffix in {'.csv', '.json'}:
            headers, records = cls.parse_table(file_path)
            return cls.format_table(path.name, headers, records)

        raise ValueError(f"Cannot handle file format: {suffix}")

    @staticmethod
    def _extract_from_pdf(file_path: str) -> str:
        """Extract text from PDF"""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ImportError("PyMuPDF required: pip install PyMuPDF")

        text_parts = []
        with fitz.open(file_path) as doc:
            for page in doc:
                text = page.get_text()
                if text.strip():
                    text_parts.append(text)

        return "\n\n".join(text_parts)

    @staticmethod
    def _extract_from_md(file_path: str) -> str:
        """Extract text from Markdown with automatic encoding detection"""
        return _read_text_with_fallback(file_path)

    @staticmethod
    def _extract_from_txt(file_path: str) -> str:
        """Extract text from TXT with automatic encoding detection"""
        return _read_text_with_fallback(file_path)

    @staticmethod
    def parse_table(file_path: str) -> tuple[List[str], List[dict]]:
        """Read a CSV or JSON record collection, rejecting ambiguous or empty data."""
        path = Path(file_path)
        try:
            content = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError(f"Invalid UTF-8 in {path.name}") from exc
        if not content.strip():
            raise ValueError(f"Empty table file: {path.name}")

        if path.suffix.lower() == '.csv':
            try:
                reader = csv.reader(io.StringIO(content, newline=''), strict=True)
                headers = next(reader)
                if not headers or any(not name.strip() for name in headers):
                    raise ValueError("CSV header must contain nonempty column names")
                if len(set(headers)) != len(headers):
                    raise ValueError("CSV header contains duplicate column names")
                records = []
                for number, values in enumerate(reader, 2):
                    if len(values) != len(headers):
                        raise ValueError(f"CSV record {number} has {len(values)} fields; expected {len(headers)}")
                    records.append(dict(zip(headers, values)))
            except (csv.Error, StopIteration) as exc:
                raise ValueError(f"Malformed CSV in {path.name}: {exc}") from exc
        elif path.suffix.lower() == '.json':
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed JSON in {path.name}: {exc.msg}") from exc
            if isinstance(data, dict):
                data = data.get('records')
            if not isinstance(data, list) or not data or any(not isinstance(row, dict) or not row for row in data):
                raise ValueError("JSON must be a nonempty array of records or an object with a records array")
            if any(any(not isinstance(key, str) or not key.strip() for key in row) for row in data):
                raise ValueError("JSON record field names must be nonempty strings")
            headers = list(dict.fromkeys(key for row in data for key in row))
            records = data
        else:
            raise ValueError(f"Unsupported table format: {path.suffix}")

        if not records:
            raise ValueError(f"Table has no records: {path.name}")
        return headers, records

    @staticmethod
    def format_table(source: str, headers: List[str], records: List[dict]) -> str:
        lines = [f"Source: {source}", f"Columns: {', '.join(headers)}"]
        for number, record in enumerate(records, 1):
            fields = ', '.join(f"{key}: {json.dumps(record.get(key), ensure_ascii=False)}" for key in headers)
            lines.append(f"Record {number}: {fields}")
        return '\n'.join(lines)

    @classmethod
    def extract_from_multiple(cls, file_paths: List[str]) -> str:
        """
        Extract text from multiple files and merge

        Args:
            file_paths: List of file paths

        Returns:
            Merged text
        """
        all_texts = []

        for i, file_path in enumerate(file_paths, 1):
            try:
                text = cls.extract_text(file_path)
                filename = Path(file_path).name
                all_texts.append(f"=== Document {i}: {filename} ===\n{text}")
            except Exception as e:
                all_texts.append(f"=== Document {i}: {file_path} (extraction failed: {str(e)}) ===")

        return "\n\n".join(all_texts)


def split_text_into_chunks(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
    chunk_overlap: Optional[int] = None
) -> List[str]:
    """
    Split text into chunks

    Args:
        text: Original text
        chunk_size: Characters per chunk
        overlap: Overlapping characters
        chunk_overlap: Optional alias for overlap

    Returns:
        List of text chunks
    """
    if chunk_overlap is not None:
        overlap = chunk_overlap

    try:
        if isinstance(chunk_size, bool):
            chunk_size = 100
        else:
            chunk_size = int(chunk_size)
    except (ValueError, TypeError):
        chunk_size = 100

    try:
        if isinstance(overlap, bool):
            overlap = 0
        else:
            overlap = int(overlap)
    except (ValueError, TypeError):
        overlap = 0

    if chunk_size <= 0:
        chunk_size = 100

    if overlap >= chunk_size or overlap < 0:
        overlap = max(0, min(overlap, chunk_size - 1))

    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        # Try to split at sentence boundaries
        if end < len(text):
            # Find nearest sentence ending
            for sep in ['。', '！', '？', '.\n', '!\n', '?\n', '\n\n', '. ', '! ', '? ']:
                last_sep = text[start:end].rfind(sep)
                if last_sep != -1 and last_sep > chunk_size * 0.3:
                    end = start + last_sep + len(sep)
                    break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Next chunk starts at overlap position, ensuring strict forward progress
        next_start = end - overlap if end < len(text) else len(text)
        start = max(start + 1, next_start)

    return chunks

