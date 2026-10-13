"""A real, chunked paper for quote and MCP tests: a PDF written by PyMuPDF, then extracted and chunked by the app."""

import uuid
from pathlib import Path

import pymupdf
from conftest import unit_vector

from app.core import papers
from app.core.chunking import chunk_blocks
from app.models import Chunk, Paper, PaperStatus
from app.providers.extraction import extract

FIRST_PARAGRAPH = (
    "Highlights are the anchor for every note in PaperLab. A note keeps the exact page and region of the passage "
    "it came from, so a citation can always jump back to the source."
)
# Narrow on purpose: it wraps over several lines, so a quote from its middle spans two of them.
SECOND_PARAGRAPH = (
    "Retrieval augmented generation grounds answers in retrieved passages. The model cites each passage it uses, "
    "and the reader can check every claim against the page it came from without leaving the reader."
)
METHOD_PARAGRAPH = (
    "The second page describes a method in enough words to form its own chunk. Coordinates are stored in PDF "
    "points with a top-left origin and converted to screen pixels only at render time."
)
TWO_LINE_QUOTE = "The model cites each passage it uses, and the reader can check every claim"
TWO_PARAGRAPH_QUOTE = "jump back to the source. Retrieval augmented generation"
TWICE = "it came from"  # once in each page-1 paragraph


def write_pdf(path: Path, intro: str = FIRST_PARAGRAPH) -> Path:
    """Page 1: "1 Introduction", a wide paragraph and a narrow one below it (one chunk, two blocks), and two small
    captions that chunking drops. Page 2: "2 Method" and one paragraph."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "1 Introduction", fontsize=14, fontname="Times-Bold")
    page.insert_textbox(pymupdf.Rect(72, 110, 540, 190), intro, fontsize=11, fontname="Times-Roman")
    page.insert_textbox(pymupdf.Rect(72, 200, 300, 400), SECOND_PARAGRAPH, fontsize=11, fontname="Times-Roman")
    # Captions too small to be chunked, repeating both quotes elsewhere on the page: in the gap between the
    # paragraphs, and at the foot of the page.
    page.insert_text((320, 170), f"Figure 1: {TWO_PARAGRAPH_QUOTE}", fontsize=7, fontname="Times-Roman")
    page.insert_text((72, 700), f"Figure 2: {TWO_LINE_QUOTE}", fontsize=7, fontname="Times-Roman")
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "2 Method", fontsize=14, fontname="Times-Bold")
    page.insert_textbox(pymupdf.Rect(72, 110, 540, 300), METHOD_PARAGRAPH, fontsize=11, fontname="Times-Roman")
    doc.save(path)
    doc.close()
    return path


async def chunked_paper(
    session, directory: Path, title: str = "Quoted paper", intro: str = FIRST_PARAGRAPH
) -> tuple[Paper, list[Chunk], list[list[float]]]:
    """A ready paper whose chunks come from the real extraction and chunking, and each chunk's vector.

    The vectors are random per call (D37): a vector derived from the text would tie with every other copy of this PDF
    in the database, even a rolled-back one still in the HNSW index, and a tie comes back in any order.
    The paper's PDF is `paper.file_path`; delete it to act as a machine without the file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    pdf = write_pdf(directory / "source.pdf", intro)
    doc = extract(pdf)
    paper = await papers.create_paper(session, "quoted.pdf", pdf.read_bytes(), directory, prefill={"title": title})
    await papers.replace_chunks(session, paper.id, chunk_blocks(doc.blocks))
    chunks = await papers.list_chunks(session, paper.id)
    vectors = [unit_vector(uuid.uuid4().hex) for _ in chunks]
    await papers.set_embeddings(session, [c.id for c in chunks], vectors, "test")
    await papers.set_status(session, paper.id, PaperStatus.READY, page_count=doc.page_count)
    return paper, chunks, vectors
