import time
import logging
import os
import tempfile
import shutil  # Import shutil for directory cleaning
from typing import List, Dict, Any, Tuple, Optional
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import queue # Import the queue module

# Import necessary modules from your project
# Assuming config, document_processing, retrieval, summarization are accessible via sys.path
# You might need to adjust these imports based on your actual project structure
try:
    from app.config.settings import DOCS_FOLDER # Keep for local runs if needed
except ImportError:
    # Define a default if config.config is not available
    DOCS_FOLDER = "docs" # Default folder name


from app.document_processing.extractors import DocumentProcessorAdapter
from app.retrieval.vector_store import Retriever
from app.summarization.summarizer import DocumentSummarizer

# Configure module-specific logger
logger = logging.getLogger(__name__)

# Add a simple print statement to confirm module loading
logger.info("main.py is being loaded.")

# Define a persistent temporary directory relative to the project root
# This assumes app.py and main.py are in the project root
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
TEMP_UPLOAD_DIR = os.path.join(PROJECT_ROOT, 'temp_uploads')
os.makedirs(TEMP_UPLOAD_DIR, exist_ok=True) # Ensure the directory exists

def clear_upload_directory():
    """Clears all files from the persistent temporary upload directory."""
    if os.path.exists(TEMP_UPLOAD_DIR):
        logger.info(f"Clearing temporary upload directory: {TEMP_UPLOAD_DIR}")
        # Iterate through all items in the directory
        for item in os.listdir(TEMP_UPLOAD_DIR):
            item_path = os.path.join(TEMP_UPLOAD_DIR, item)
            try:
                # Check if it's a file or a symbolic link and remove it
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                    logger.debug(f"Deleted file/link: {item_path}")
                # Check if it's a directory and remove it and its contents
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    logger.debug(f"Deleted directory: {item_path}")
            except Exception as e:
                logger.error(f"Error deleting {item_path}: {e}", exc_info=True)
    else:
         logger.info(f"Temporary upload directory does not exist, no need to clear: {TEMP_UPLOAD_DIR}")


def process_uploaded_files(uploaded_files) -> List[Dict[str, Any]]:
    """
    Processes a list of files uploaded via Streamlit.
    Saves them into a persistent temporary folder and uses the DocumentProcessorAdapter
    to process that folder.

    Args:
        uploaded_files: List of Streamlit UploadedFile objects.
                        Type hint is omitted here to avoid needing Streamlit import at top level.

    Returns:
        List of dictionaries with original extraction results, including chunk_size.
    """
    # Import streamlit here, as it's used for st.warning
    import streamlit as st

    start_time = time.time()
    logger.info(f"Starting processing for {len(uploaded_files)} uploaded files.")

    # Save all uploaded files into the persistent temporary directory
    logger.info(f"Saving files to persistent temporary directory: {TEMP_UPLOAD_DIR}")
    saved_files_paths = []
    for uploaded_file in uploaded_files:
        # Create a safe path within the temporary directory
        # Use uploaded_file.name directly, ensuring it's joined with the base temp dir
        file_path = os.path.join(TEMP_UPLOAD_DIR, uploaded_file.name)
        # Write the file content to the temporary path
        try:
            with open(file_path, "wb") as f:
                f.write(uploaded_file.getvalue())
            logger.debug(f"Saved uploaded file '{uploaded_file.name}' to '{file_path}'")
            saved_files_paths.append(file_path)
        except Exception as e:
            logger.error(f"Error saving uploaded file '{uploaded_file.name}' to temporary directory: {e}", exc_info=True)
            # Log a warning in Streamlit if a file couldn't be saved
            st.warning(f"Could not save uploaded file '{uploaded_file.name}' temporarily. It will be skipped.")

    # Use the DocumentProcessorAdapter to process the entire temporary folder
    processor = DocumentProcessorAdapter()
    # Call process_folder with the persistent temporary directory path
    # The process_folder method returns the list of extraction results
    extraction_results = processor.process_folder(TEMP_UPLOAD_DIR)

    end_time = time.time()
    logger.info(f"Finished processing uploaded files (saving and initial extraction) in {end_time - start_time:.2f} seconds.")
    # The extraction_results list now contains dictionaries with 'filename', 'text', 'error', etc.
    return extraction_results


def setup_retrieval_system(extraction_results: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Retriever]:
    """
    Sets up the retrieval system (vector store) from extraction results.

    Args:
        extraction_results: List of dictionaries from document extraction.
                            Should contain 'filename' and 'text'.

    Returns:
        A tuple containing:
        - The updated extraction_results list (with 'chunk_size' populated by Retriever).
        - An initialized Retriever instance.
    """
    start_time = time.time()
    logger.info("Setting up retrieval system.")
    try:
        retriever = Retriever()
        # create_from_documents takes extraction results, chunks text, embeds, and builds the DB.
        # It also updates the extraction_results list with the 'chunk_size' for each document.
        updated_extraction_results = retriever.create_from_documents(extraction_results)
        end_time = time.time()
        logger.info(f"Retriever setup complete in {end_time - start_time:.2f} seconds.")
        return updated_extraction_results, retriever
    except Exception as e:
        end_time = time.time()
        logger.error(f"Error during retrieval system setup: {e}", exc_info=True)
        # If retrieval setup fails, the summarization cannot proceed.
        # Re-raise the exception so the Streamlit app can catch and display it.
        raise


def summarize_extracted_documents(extraction_results: List[Dict[str, Any]], retriever: Retriever, update_queue: queue.Queue) -> List[Dict[str, Any]]:
    """
    Summarizes documents based on extraction results and a configured retriever,
    reporting progress via a queue.

    Args:
        extraction_results: List of dictionaries from document extraction (should include chunk_size
                            populated by setup_retrieval_system).
                            EXPECTED to be a list containing *one* document result dictionary
                            when called from the Streamlit app for individual file processing.
        retriever: An initialized Retriever instance.
        update_queue: A thread-safe queue.Queue object to put progress updates into.

    Returns:
        A list of dictionaries, each containing the summary result for a file.
        Each dictionary includes:
        - 'filename': The name of the file.
        - 'success': Boolean indicating if summarization was successful.
        - 'summary': The generated summary string (if successful), or None.
        - 'error': An error message string (if not successful), or None.
        - 'processing_time': Time taken for summarization of this file.
        - 'status': Current processing status (e.g., 'completed', 'error').
    """
    # This function is designed to handle a list of results, but the Streamlit app
    # calls it with a list containing a single document result for parallel processing.
    # The internal logic iterates through the provided list.

    start_time = time.time()
    logger.info(f"Starting summarization for {len(extraction_results)} document(s) in background.")

    # Initialize the summarizer with the retriever
    summarizer = DocumentSummarizer(retriever)

    results = [] # List to store results for each document processed by this call

    # Filter out results that failed extraction or have no text/chunks from the input list
    # Summarization requires extracted text and successful chunking (chunk_size > 0)
    summarizable_input_results = [
        res for res in extraction_results
        if res.get('text') and res.get('chunk_size', 0) > 0 and res.get('error') is None
    ]
    skipped_input_results = [
        res for res in extraction_results
        if res not in summarizable_input_results
    ]

    if skipped_input_results:
        logger.warning(f"Skipping summarization for {len(skipped_input_results)} input file(s) due to extraction errors or no text/chunks.")
        for res in skipped_input_results:
            # Add entries for skipped files to the results list
            skipped_result = {
                'filename': res.get('filename', 'unknown'),
                'success': False,
                'summary': None,
                'error': res.get('error', 'Extraction failed or no text/chunks'),
                'processing_time': 0, # No summarization time for skipped files
                'status': 'skipped' # Add status for skipped files
            }
            results.append(skipped_result)
            # Put status update into the queue
            update_queue.put({'filename': skipped_result['filename'], 'status': skipped_result['status'], 'result_data': skipped_result})


    # Process only the summarizable documents from the input list
    for result in summarizable_input_results:
        file_start_time = time.time()
        filename = result.get('filename', 'unknown')
        language = result.get('language', 'en')
        chunk_size = result.get('chunk_size', 0) # Should be > 0 for summarizable_input_results

        logger.info(f"Background: Summarizing document: {filename}")
        # Put status update into the queue
        update_queue.put({'filename': filename, 'status': 'summarizing'})

        try:
            # Call the summarizer for this single document
            summary = summarizer.summerize_document(filename, language, chunk_size)

            file_end_time = time.time()
            logger.info(f"Background: Finished summarizing {filename} in {file_end_time - file_start_time:.2f} seconds.")
            summary_result = {
                'filename': filename,
                'success': True,
                'summary': summary, # Return the summary string
                'error': None,
                'processing_time': file_end_time - file_start_time,
                'status': 'completed' # Add completed status
            }
            results.append(summary_result) # Add to the results list for this call
            # Put status update into the queue
            update_queue.put({'filename': filename, 'status': summary_result['status'], 'result_data': summary_result})

        except Exception as e:
            file_end_time = time.time()
            error_msg = str(e)
            logger.error(f"Background: Error summarizing document {filename}: {e}", exc_info=True)
            error_result = {
                'filename': filename,
                'success': False,
                'summary': None,
                'error': error_msg,
                'processing_time': file_end_time - file_start_time,
                'status': 'error' # Add error status
            }
            results.append(error_result) # Add to the results list for this call
            # Put status update into the queue
            update_queue.put({'filename': filename, 'status': error_result['status'], 'result_data': error_result})

    end_time = time.time()
    logger.info(f"Background: Finished processing {len(extraction_results)} document(s) in {end_time - start_time:.2f} seconds.")
    # Return the list of results processed by this specific call
    return results
