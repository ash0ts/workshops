import json
import pathlib
from copy import deepcopy
from hashlib import md5
from typing import Any
import re

import frontmatter
import markdown
import weave
from bs4 import BeautifulSoup
from IPython.display import Markdown, display
from litellm import acompletion, decode, encode
from markdownify import markdownify
from nbconvert import MarkdownExporter as NBMarkdownExporter
from nbformat import reads as readnb
from nbformat.validator import normalize as normalizenb
from nltk.tokenize import sent_tokenize
from traitlets.config import Config as NBConfig
from tree_sitter_languages import get_parser


def mdify(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for nav in soup.find_all("nav"):
        nav.decompose()
    for button in soup.find_all("button"):
        button.decompose()
    for img in soup.find_all("img"):
        img.decompose()
    for script in soup.find_all("script"):
        script.decompose()
    for style in soup.find_all("style"):
        style.decompose()
    for svg in soup.find_all("svg"):
        svg.decompose()
    for a_tag in soup.find_all("a"):
        a_tag.replace_with(a_tag.text)
    article = soup.select("main article div.theme-doc-markdown.markdown")
    combined_html = "".join(str(art) for art in article)
    return markdownify(combined_html, heading_style="ATX", wrap=True)


def convert_contents_to_text(contents: str, file_type: str = "pdf") -> str:
    """
    Converts the given content to plain text, with special handling for PDFs.

    Args:
        contents: A string containing the content
        file_type: Type of content ("markdown", "pdf", etc.)

    Returns:
        A string containing the plain text extracted from the content
    """
    if file_type == "pdf":
        # Clean up PDF-specific artifacts
        text = contents
        
        # Remove page numbers and headers
        text = re.sub(r'\n.*?\| Q\d 20\d{2} Form 10-Q \|.*?\n', '\n', text)
        
        # Standardize whitespace in financial tables
        text = re.sub(r'\$\s+', '$ ', text)  # Standardize spacing after dollar signs
        text = re.sub(r'(\d),(\d)', r'\1\2', text)  # Remove commas in numbers
        
        # Clean up common PDF artifacts
        text = re.sub(r'', '', text)  # Remove invalid characters
        text = re.sub(r'\s{2,}', ' ', text)  # Collapse multiple spaces
        text = re.sub(r'\n{3,}', '\n\n', text)  # Collapse multiple newlines
        
        return text.strip()
    else:
        # Original markdown handling
        _, content = frontmatter.parse(contents)
        markdown_document = markdown.markdown(
            content,
            extensions=[
                "toc",
                "pymdownx.extra",
                "pymdownx.blocks.admonition",
                "pymdownx.magiclink",
                "pymdownx.blocks.tab",
                "pymdownx.pathconverter",
                "pymdownx.saneheaders",
                "pymdownx.striphtml",
                "pymdownx.highlight",
                "pymdownx.pathconverter",
                "pymdownx.escapeall",
            ],
        )
        soup = BeautifulSoup(markdown_document, "html.parser")
        return soup.get_text()


def tokenize_text(text: str, model: str = "gpt-4o-mini") -> list[str]:
    encoded = encode(model=model, text=text)
    decoded = [decode(model=model, tokens=[enc]) for enc in encoded]
    return decoded


def chunk_simple(content, chunk_size=300, model="gpt-4o-mini"):
    sentences = sent_tokenize(content)
    chunks = []
    current_chunk = []
    current_length = 0

    for sentence in sentences:
        words = tokenize_text(sentence, model=model)

        # Check if adding this sentence would exceed chunk_size
        if current_length + len(words) > chunk_size and current_chunk:
            # Save current chunk and start a new one
            chunks.append("".join(current_chunk))
            current_chunk = []
            current_length = 0

        # Add sentence to current chunk
        current_chunk.extend(words)
        current_length += len(words)

    # Don't forget the last chunk if it exists
    if current_chunk:
        chunks.append("".join(current_chunk))

    return chunks


def make_id(content):
    return md5(content.encode()).hexdigest()


def extract_text_from_node(node):
    """Extract text content from a node and its children."""
    if not node.children and node.text:
        return node.text.decode("utf-8")

    return "".join(extract_text_from_node(child) for child in node.children)


def chunk_by_headings(root_node):
    """Split markdown into chunks based on headings."""
    chunks = []
    current_chunk = []
    current_heading = None

    for node in root_node.children:
        if node.type == "atx_heading":
            # When we find a heading, save the previous chunk if it exists
            if current_heading is not None:
                chunks.append("".join(current_chunk).strip())

            # Start a new chunk with this heading
            current_heading = extract_text_from_node(node)
            current_chunk = [current_heading + "\n\n"]
        else:
            # Add content to current chunk
            if current_heading is not None:
                current_chunk.append(extract_text_from_node(node))

    # Don't forget to add the last chunk
    if current_heading is not None:
        chunks.append("".join(current_chunk).strip())

    return chunks


def chunk_markdown(content, chunk_size=300):
    parser = get_parser("markdown")
    root = parser.parse(content.encode())
    chunks = chunk_by_headings(root.root_node)
    chunks = [chunk_simple(chunk, chunk_size) for chunk in chunks]
    return [doc for chunk in chunks for doc in chunk]


def collect_imports(node):
    imports = []
    for child in node.children:
        if child.type in ("import_statement", "import_from_statement"):
            imports.append(child)
    return imports


def build_import_map(import_nodes):
    import_map = {}
    for imp in import_nodes:
        imp_text = imp.text.decode("utf-8")
        if imp.type == "import_from_statement":
            for child in imp.children[3:]:
                if child.type == "dotted_name":
                    name = child.text.decode("utf-8")
                    import_map[name] = imp_text
                elif child.type == "aliased_import":
                    alias = child.children[2].text.decode("utf-8")
                    import_map[alias] = imp_text
        elif imp.type == "import_statement":
            for child in imp.children:
                if child.type == "dotted_name":
                    name = child.text.decode("utf-8")
                    import_map[name] = imp_text
                elif child.type == "aliased_import":
                    alias = child.children[2].text.decode("utf-8")
                    import_map[alias] = imp_text
    return import_map


def collect_module_variables(node):
    module_variables = []
    for child in node.children:
        if child.type == "expression_statement":
            assignment = child.children[0]
            if assignment.type == "assignment":
                target = assignment.child_by_field_name("left")
                if target and target.type == "identifier":
                    var_name = target.text.decode("utf-8")
                    module_variables.append(var_name)
    return module_variables


def collect_identifiers(node, identifiers):
    if node.type == "identifier":
        identifiers.add(node.text.decode("utf-8"))
    elif node.type in ("function_definition", "class_definition"):
        # Collect identifiers from parameters and return type
        parameters = node.child_by_field_name("parameters")
        if parameters:
            collect_identifiers(parameters, identifiers)
        return_type = node.child_by_field_name("return_type")
        if return_type:
            collect_identifiers(return_type, identifiers)
        # Do not process the body
    else:
        for child in node.children:
            collect_identifiers(child, identifiers)


def collect_identifiers_from_type_annotations(node, identifiers):
    if node.type in ("type", "type_identifier", "identifier"):
        identifiers.add(node.text.decode("utf-8"))
    else:
        for child in node.children:
            collect_identifiers_from_type_annotations(child, identifiers)


def process_decorated_definition(node, context, import_map, module_variables):
    chunks = []
    identifiers = set()

    # Collect identifiers from the entire decorated_definition node
    collect_identifiers(node, identifiers)

    # Find the inner function or class definition
    func_def = None
    for child in node.children:
        if child.type in ("function_definition", "class_definition"):
            func_def = child
            break

    if func_def:
        func_name_node = func_def.child_by_field_name("name")
        func_name = func_name_node.text.decode("utf-8")
        func_code = node.text.decode("utf-8")

        # No need to collect identifiers again from func_def, already collected from node

        # Determine which identifiers are imports or module variables
        used_imports = []
        used_module_vars = []

        for ident in identifiers:
            if ident in import_map:
                used_imports.append(import_map[ident])
            if ident in module_variables:
                used_module_vars.append(ident)

        func_context = {}

        if used_imports:
            func_context["imports"] = list(set(used_imports))

        if used_module_vars:
            func_context["module_variables"] = used_module_vars

        if "parent_class" in context:
            func_context["parent_class"] = context["parent_class"]

        if "parent_function" in context:
            func_context["parent_function"] = context["parent_function"]

        if not func_context:
            func_context = None

        result = {
            "type": "function" if func_def.type == "function_definition" else "class",
            "name": func_name,
            "definition": func_code,
            "context": func_context,
        }

        if "parent_class" in context and func_def.type == "function_definition":
            result["type"] = "method"

        chunks.append(result)
    return chunks


def process_function(node, context, import_map, module_variables):
    chunks = []
    func_name_node = node.child_by_field_name("name")
    func_name = func_name_node.text.decode("utf-8")

    func_code = node.text.decode("utf-8")

    # Collect identifiers used in the function, excluding nested functions
    identifiers = set()
    func_body = node.child_by_field_name("body")
    collect_identifiers(func_body, identifiers)

    # Collect identifiers from parameter type annotations and return type
    parameters = node.child_by_field_name("parameters")
    if parameters:
        collect_identifiers_from_type_annotations(parameters, identifiers)
    return_type = node.child_by_field_name("return_type")
    if return_type:
        collect_identifiers_from_type_annotations(return_type, identifiers)

    # Determine which identifiers are imports or module variables
    used_imports = []
    used_module_vars = []

    for ident in identifiers:
        if ident in import_map:
            used_imports.append(import_map[ident])
        if ident in module_variables:
            used_module_vars.append(ident)

    # Build the context
    func_context = {}

    if used_imports:
        func_context["imports"] = list(set(used_imports))

    if used_module_vars:
        func_context["module_variables"] = used_module_vars

    if "parent_class" in context:
        func_context["parent_class"] = context["parent_class"]

    if "parent_function" in context:
        func_context["parent_function"] = context["parent_function"]

    if not func_context:
        func_context = None

    result = {
        "type": "function",
        "name": func_name,
        "definition": func_code,
        "context": func_context,
    }

    if "parent_class" in context:
        result["type"] = "method"

    chunks.append(result)

    # Process any nested functions
    for child in func_body.children:
        if child.type == "function_definition":
            context_copy = context.copy()
            context_copy["parent_function"] = func_name
            chunks.extend(
                process_function(child, context_copy, import_map, module_variables)
            )
        elif child.type == "decorated_definition":
            chunks.extend(
                process_decorated_definition(
                    child, context, import_map, module_variables
                )
            )
    return chunks


def process_class(node, context, import_map, module_variables):
    chunks = []
    class_name_node = node.child_by_field_name("name")
    class_name = class_name_node.text.decode("utf-8")

    class_body = node.child_by_field_name("body")

    class_attributes = []
    init_method = None
    other_methods = []
    identifiers = set()  # Collect identifiers used in class attributes

    for child in class_body.children:
        if child.type == "expression_statement":
            assignment = child.children[0]
            if assignment.type == "assignment":
                class_attributes.append(assignment.text.decode("utf-8"))
                # Collect identifiers from the assignment
                collect_identifiers(assignment, identifiers)
            elif assignment.type == "typed_parameter":
                # Handles class attributes with type annotations
                class_attributes.append(assignment.text.decode("utf-8"))
                collect_identifiers_from_type_annotations(assignment, identifiers)
        elif child.type == "function_definition":
            func_name_node = child.child_by_field_name("name")
            func_name = func_name_node.text.decode("utf-8")
            if func_name == "__init__":
                init_method = child
            else:
                other_methods.append(child)
        elif child.type == "decorated_definition":
            func_def = None
            for c in child.children:
                if c.type == "function_definition":
                    func_def = c
                    break
            if func_def:
                func_name_node = func_def.child_by_field_name("name")
                func_name = func_name_node.text.decode("utf-8")
                if func_name == "__init__":
                    init_method = child
                else:
                    other_methods.append(child)

    # Determine which identifiers are imports or module variables
    used_imports = []
    used_module_vars = []

    for ident in identifiers:
        if ident in import_map:
            used_imports.append(import_map[ident])
        if ident in module_variables:
            used_module_vars.append(ident)

    # Build the context
    class_context = {}

    if used_imports:
        class_context["imports"] = list(set(used_imports))

    if used_module_vars:
        class_context["module_variables"] = used_module_vars

    if not class_context:
        class_context = None

    # Build the class definition
    class_def_lines = ["class " + class_name + ":"]

    for attr in class_attributes:
        class_def_lines.append("    " + attr)

    if init_method:
        init_code = "\n\n    " + init_method.text.decode("utf-8")
        init_lines = init_code.split("\n")
        for line in init_lines:
            class_def_lines.append(line)

    class_definition = "\n".join(class_def_lines)

    chunk = {
        "type": "class",
        "name": class_name,
        "definition": class_definition,
        "context": class_context,
    }

    chunks.append(chunk)

    for method in other_methods:
        context_copy = context.copy()
        context_copy["parent_class"] = class_name
        if method.type == "decorated_definition":
            chunks.extend(
                process_decorated_definition(
                    method, context_copy, import_map, module_variables
                )
            )
        else:
            chunks.extend(
                process_function(method, context_copy, import_map, module_variables)
            )
    return chunks


def process_root(node, import_map, module_variables):
    chunks = []
    module_code_lines = []
    module_imports = []
    for child in node.children:
        if child.type in ("import_statement", "import_from_statement"):
            import_text = child.text.decode("utf-8")
            module_code_lines.append(import_text)
            module_imports.append(import_text)
        elif child.type == "class_definition":
            chunks.extend(
                process_class(
                    child,
                    context={},
                    import_map=import_map,
                    module_variables=module_variables,
                )
            )
        elif child.type == "function_definition":
            chunks.extend(
                process_function(
                    child,
                    context={},
                    import_map=import_map,
                    module_variables=module_variables,
                )
            )
        elif child.type == "decorated_definition":
            chunks.extend(
                process_decorated_definition(
                    child,
                    context={},
                    import_map=import_map,
                    module_variables=module_variables,
                )
            )
    return chunks


def convert_chunks_to_strs(chunks):
    str_chunks = []
    for chunk in chunks:
        chunk_str = ""
        chunk_context = chunk["context"]
        if chunk_context is not None and chunk["type"] != "module":
            chunk_context_str = ""
            if "imports" in chunk_context:
                chunk_context_str += "\n".join(chunk_context["imports"]) + "\n"
            if "module_variables" in chunk_context:
                chunk_context_str += "\n".join(chunk_context["module_variables"]) + "\n"
            if "parent_class" in chunk_context:
                chunk_context_str += (
                    f"\nclass: {chunk_context['parent_class']}" + "\n    # ... (more)\n"
                )
            if "parent_function" in chunk_context:
                chunk_context_str += (
                    f"\ndef: {chunk_context['parent_function']}"
                    + "\n    # ... (more)\n"
                )
            chunk_str += f"{chunk_context_str}" + "\n    "
        if chunk["type"] == "class":
            chunk_str = chunk_str[:-4]
        chunk_str += f"{chunk['definition']}"
        str_chunks.append(chunk_str)
    return str_chunks


def chunk_source_code(content: str, chunk_size: int = 300):
    parser = get_parser("python")
    tree = parser.parse(content.encode())
    import_nodes = collect_imports(tree.root_node)
    import_map = build_import_map(import_nodes)
    module_variables = collect_module_variables(tree.root_node)
    chunks = process_root(tree.root_node, import_map, module_variables)
    chunks_strs = convert_chunks_to_strs(chunks)
    chunks_strs = list(filter(lambda x: len(x.strip().splitlines()) > 1, chunks_strs))
    chunks_strs = [chunk_simple(chunk, chunk_size) for chunk in chunks_strs]
    return [doc for chunk in chunks_strs for doc in chunk]


def read_notebook(content):
    notebook = readnb(content, as_version=4)
    _, notebook = normalizenb(notebook, version=4, strip_invalid_metadata=True)
    conf = NBConfig()
    conf.MarkdownExporter.preprocessors = [
        "nbconvert.preprocessors.ClearOutputPreprocessor"
    ]
    md_exporter = NBMarkdownExporter(config=conf, template="classic")
    body, _ = md_exporter.from_notebook_node(notebook)
    return body


def chunk_notebook(content, chunk_size=300):
    md_notebook = read_notebook(content)
    md_notebook = markdown.markdown(
        md_notebook,
        extensions=[
            "toc",
            "pymdownx.extra",
            "pymdownx.blocks.admonition",
            "pymdownx.magiclink",
            "pymdownx.blocks.tab",
            "pymdownx.pathconverter",
            "pymdownx.saneheaders",
            "pymdownx.striphtml",
            "pymdownx.highlight",
            "pymdownx.pathconverter",
            "pymdownx.escapeall",
        ],
    )
    md_notebook = mdify(md_notebook)
    chunks = chunk_markdown(md_notebook, chunk_size)
    return chunks


def format_doc(doc, with_ids=False, max_length=None):
    doc_str = ""
    for k, v in doc.items():
        if k not in ["text", "chunk", "content"]:
            if not with_ids:
                if k in ["doc_id", "chunk_id"]:
                    continue
            doc_str += f"- {k}: {v}\n"
    doc_str += "\n\n"
    
    # Check if file_type is directly in doc or in metadata
    file_type = doc.get("file_type") or doc.get("metadata", {}).get("file_type")
    
    if file_type == "python":
        if max_length:
            doc_str += doc["text"][:max_length]
        else:
            doc_str += doc["text"]
    else:
        if max_length:
            doc_str += doc["chunk"][:max_length]
        else:
            doc_str += doc["chunk"]
    if max_length:
        doc_str += "\n\n...\n\n"
    return doc_str


def render_doc(doc, max_length=None):
    doc_str = format_doc(doc, with_ids=True, max_length=max_length)
    doc_str += "\n\n---\n\n"
    return display(Markdown(doc_str))


def printmd(text):
    display(Markdown(text))


def load_dataset(docs_root):
    files = pathlib.Path(docs_root).rglob("*.jsonl")
    docs = []
    for file in files:
        for line in file.read_text().splitlines():
            docs.append(json.loads(line))
    return docs


def chunk_pdf(content: str, chunk_size: int = 500) -> list[str]:
    """
    Chunks PDF content more intelligently by respecting table boundaries and financial statements.
    
    Args:
        content: Raw text content extracted from PDF
        chunk_size: Target size for chunks in tokens
        
    Returns:
        List of text chunks
    """
    # Split into sections based on common financial document headers
    section_markers = [
        "CONSOLIDATED STATEMENTS OF",
        "NOTES TO",
        "PART ",
        "Item ",
        "TABLE OF CONTENTS"
    ]
    
    # First split by major sections
    sections = []
    current_section = []
    
    for line in content.splitlines():
        # Check if line starts new section
        is_new_section = any(line.strip().upper().startswith(marker) for marker in section_markers)
        
        if is_new_section and current_section:
            sections.append("\n".join(current_section))
            current_section = [line]
        else:
            current_section.append(line)
    
    if current_section:
        sections.append("\n".join(current_section))

    # Further chunk each section while preserving table structure
    chunks = []
    for section in sections:
        # Detect if section contains tabular data
        has_table = bool(re.search(r'\$\s*[\d,]+', section))
        
        if has_table:
            # Keep table sections together
            chunks.append(section)
        else:
            # Use sentence-based chunking for narrative text
            sentences = sent_tokenize(section) 
            current_chunk = []
            current_length = 0
            
            for sentence in sentences:
                sentence_tokens = tokenize_text(sentence)
                if current_length + len(sentence_tokens) > chunk_size and current_chunk:
                    chunks.append(" ".join(current_chunk))
                    current_chunk = [sentence]
                    current_length = len(sentence_tokens)
                else:
                    current_chunk.append(sentence)
                    current_length += len(sentence_tokens)
            
            if current_chunk:
                chunks.append(" ".join(current_chunk))

    return chunks


def chunk_dataset(ds, chunk_size=500):
    all_chunks = []
    for doc in ds:
        doc_id = make_id(doc["content"])
        if doc["file_type"] == "python":
            chunks = chunk_source_code(doc["content"], chunk_size=chunk_size)
            for i, chunk in enumerate(chunks):
                doc_chunk = deepcopy(doc)
                del doc_chunk["content"]
                doc_chunk["chunk"] = chunk
                doc_chunk["text"] = chunk
                doc_chunk["chunk_id"] = make_id(chunk)
                doc_chunk["doc_id"] = doc_id
                doc_chunk["chunk_number"] = i
                all_chunks.append(doc_chunk)
        elif doc["file_type"] == "notebook":
            chunks = chunk_notebook(doc["content"], chunk_size=chunk_size)
            for i, chunk in enumerate(chunks):
                doc_chunk = deepcopy(doc)
                del doc_chunk["content"]
                doc_chunk["chunk"] = chunk
                doc_chunk["text"] = chunk
                doc_chunk["chunk_id"] = make_id(chunk)
                doc_chunk["doc_id"] = doc_id
                doc_chunk["chunk_number"] = i
                all_chunks.append(doc_chunk)
        elif doc["file_type"] == "pdf":
            # Use PDF-specific chunking
            chunks = chunk_pdf(doc["content"], chunk_size=chunk_size)
            for i, chunk in enumerate(chunks):
                doc_chunk = deepcopy(doc)
                del doc_chunk["content"]
                doc_chunk["chunk"] = chunk
                doc_chunk["text"] = convert_contents_to_text(chunk, file_type="pdf")
                doc_chunk["chunk_id"] = make_id(chunk)
                doc_chunk["doc_id"] = doc_id
                doc_chunk["chunk_number"] = i
                all_chunks.append(doc_chunk)
        else:
            chunks = chunk_markdown(doc["content"], chunk_size=chunk_size)
            for i, chunk in enumerate(chunks):
                doc_chunk = deepcopy(doc)
                del doc_chunk["content"]
                doc_chunk["chunk"] = chunk
                doc_chunk["text"] = convert_contents_to_text(chunk, file_type="markdown")
                doc_chunk["chunk_id"] = make_id(chunk)
                doc_chunk["doc_id"] = doc_id
                doc_chunk["chunk_number"] = i
                all_chunks.append(doc_chunk)
    return all_chunks


@weave.op
async def run_llm(
    model: str = "gpt-4o-mini",
    temperature: float = 0.1,
    messages: list[dict[str, Any]] = None,
) -> str:
    response = await acompletion(
        model=model, temperature=temperature, messages=messages
    )
    return response.choices[0].message.content

def prepare_documents_for_pinecone(documents):
    """
    Process document chunks into a format suitable for Pinecone indexing.
    
    Args:
        documents: List of document chunks with nested metadata
        
    Returns:
        List of documents with flattened structure and simple metadata types
    """
    processed_docs = []
    for doc in documents:
        # Only include metadata fields that are strings, numbers, booleans, or lists of strings
        processed_doc = {
            "chunk": doc['chunk'],
            "text": doc['text'],
            "source": doc['metadata']['source'],
            "file_type": doc['metadata']['file_type'],
            "chunk_number": float(doc['chunk_number']),  # Convert to number
            "chunk_id": doc['chunk_id'],
            "doc_id": doc['doc_id']
        }
        processed_docs.append(processed_doc)
    return processed_docs
