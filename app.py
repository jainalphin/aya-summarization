# app.py
import streamlit as st
import os
import sys
import time
import logging
import concurrent.futures # Use concurrent.futures explicitly
from concurrent.futures import ThreadPoolExecutor, Future # Import Future
import shutil # Import shutil for clearing directory
import queue # Import the queue module

# Add the project root to the sys.path to allow importing modules like config, document_processing, etc.
# This assumes app.py is in the project root directory.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__)))
if project_root not in sys.path:
    sys.path.append(project_root)

# Initialize ThreadPoolExecutor - reuse it across reruns
# Max workers can be adjusted based on expected load and I/O vs CPU bound tasks
if 'executor' not in st.session_state:
    st.session_state.executor = ThreadPoolExecutor(max_workers=os.cpu_count()*2) # Adjust max_workers as needed

# Initialize a thread-safe queue for communication from background threads
# This queue will store updates about file processing status
if 'update_queue' not in st.session_state:
    st.session_state.update_queue = queue.Queue()


try:
    # Import the necessary functions and constants from your main script
    # Ensure your main.py has the clear_upload_directory function and TEMP_UPLOAD_DIR constant
    # and that summarize_extracted_documents accepts the update_queue argument.
    from main import process_uploaded_files, setup_retrieval_system, summarize_extracted_documents, clear_upload_directory, TEMP_UPLOAD_DIR

    # Configure Streamlit's logging to match your application's settings
    logging.basicConfig(level='INFO', format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logger = logging.getLogger(__name__)
    logger.info("Streamlit app started and logging configured.")

    # Flag to check if modules were imported successfully
    modules_loaded = True

except ImportError as e:
    st.error(f"Could not import application modules. Please ensure your project structure is correct and dependencies are installed.")
    st.error(f"ImportError: {e}")
    logger = logging.getLogger(__name__)
    logger.error(f"Failed to import application modules: {e}", exc_info=True)
    modules_loaded = False # Set flag to False if imports fail


# --- Streamlit App Configuration ---
st.set_page_config(
    page_title="Aya Insight Document Summarizer",
    page_icon="塘",
    layout="wide"
)

# --- Session State Initialization ---
# Initialize session state variables if they don't exist
if 'api_key_entered' not in st.session_state:
    st.session_state.api_key_entered = False
# Initialize summary_results as a dictionary to store results per file
# Key: filename, Value: {'status': str, 'summary': str | None, 'error': str | None, 'success': bool, ...}
if 'summary_results' not in st.session_state or st.session_state.summary_results is None:
    st.session_state.summary_results = {}
if 'selected_filename' not in st.session_state:
    st.session_state.selected_filename = None
# Add state variables to track the background task for each file
# Key: filename, Value: concurrent.futures.Future object
if 'processing_futures' not in st.session_state:
    st.session_state.processing_futures = {}
# Overall summarizing flag
if 'summarizing' not in st.session_state:
    st.session_state.summarizing = False
if 'overall_error' not in st.session_state: # For errors in initial steps (extraction, setup)
     st.session_state.overall_error = None


# --- API Key Input Section ---
if not st.session_state.api_key_entered:
    st.title("白 Enter Your Cohere API Key to Unlock")
    api_key = st.text_input("Cohere API Key", type="password", help="Enter your Cohere API key to use the summarization service.")

    if st.button("Unlock"):
        if api_key:
            os.environ["COHERE_API_KEY"] = api_key # Set the environment variable
            st.session_state.api_key_entered = True
            st.success("API Key accepted. You can now upload documents.")
            st.rerun() # Rerun the app to show the main content
        else:
            st.warning("Please enter your Cohere API key.")

# --- Main Application Content (Unlocked) ---
if st.session_state.api_key_entered and modules_loaded:
    st.title("塘 Aya Insight Document Summarizer")
    st.markdown("""
    Upload one or more PDF or image files to get a structured summary for each document.
    """)

    # --- File Uploader ---
    uploaded_files = st.file_uploader(
        "Choose Document Files",
        type=["pdf", "png", "jpg", "jpeg", "tiff", "bmp", "gif"], # Added image types
        accept_multiple_files=True,
        help="You can upload multiple PDF or image documents here."
    )

    # --- Summarize Button and Logic ---
    # Disable the button if summarization is already in progress
    if uploaded_files: # Only show button if files are uploaded
        st.info(f"You have uploaded {len(uploaded_files)} file(s).")

        # Use a unique key for the button based on the summarizing state
        button_key = "summarize_button_processing" if st.session_state.summarizing else "summarize_button_ready"

        if st.button("Generate Summaries", key=button_key, disabled=st.session_state.summarizing):
            st.session_state.selected_filename = None # Reset selected file on new summary generation
            st.session_state.summary_results = {} # Clear previous results before starting new process
            st.session_state.processing_futures = {} # Clear previous futures
            st.session_state.summarizing = True # Set summarizing flag
            st.session_state.overall_error = None # Clear previous overall errors
            st.session_state.update_queue = queue.Queue() # Create a new queue for this batch

            if not uploaded_files:
                st.warning("Please upload at least one file before generating summaries.")
                st.session_state.summarizing = False # Reset flag if no files
                st.rerun() # Rerun to clear spinner if no files
            else:
                st.subheader("Processing Documents...")
                # Clear the temporary upload directory from previous runs
                clear_upload_directory()
                logger.info("Cleared previous upload directory content.")

                # --- Initial Processing (Extraction and Retrieval Setup) ---
                # This part still runs in the main Streamlit thread
                logger.info(f"Calling process_uploaded_files with {len(uploaded_files)} files.")
                try:
                    # Save files and perform initial extraction
                    extraction_results = process_uploaded_files(uploaded_files)
                    logger.info(f"Finished document extraction. {len(extraction_results)} results obtained.")

                    # Initialize status for all uploaded files (even those that might fail extraction)
                    # This ensures all uploaded files get a tile.
                    uploaded_filenames = [f.name for f in uploaded_files]
                    for filename in uploaded_filenames:
                         # Find the corresponding extraction result to get potential early errors
                         initial_res = next((res for res in extraction_results if res.get('filename') == filename), {})
                         status = 'waiting'
                         error = initial_res.get('error')
                         if error:
                             status = 'extraction_error'
                             st.warning(f"Extraction failed for {filename}: {error}")

                         st.session_state.summary_results[filename] = {
                             'status': status,
                             'summary': None,
                             'error': error,
                             'success': False
                         }

                    # Filter for files that had successful extraction (have text)
                    summarizable_extraction_results = [res for res in extraction_results if res.get('text')]

                    if not summarizable_extraction_results:
                        st.error("No text could be extracted from the uploaded files. Please check the file formats.")
                        logger.error("No text extracted from any uploaded file.")
                        st.session_state.summarizing = False # Reset flag
                        st.rerun() # Rerun to clear spinner and show error
                        # st.stop() # Don't stop, allow user to try again


                    # Setup retrieval system (Vector Store and Embedding)
                    logger.info("Calling setup_retrieval_system.")
                    # setup_retrieval_system needs results with text
                    extraction_results_with_chunks, retriever = setup_retrieval_system(summarizable_extraction_results)
                    logger.info("Retriever system setup complete.")

                    # Check if chunking was successful for summarizable files
                    final_summarizable_results = []
                    for res in extraction_results_with_chunks:
                         filename = res.get('filename', 'unknown')
                         if res.get('chunk_size', 0) > 0:
                             final_summarizable_results.append(res)
                             # Update status for files that are ready for summarization
                             if filename in st.session_state.summary_results:
                                  st.session_state.summary_results[filename]['status'] = 'queued' # Ready for background task
                         else:
                             # Update status for files that failed chunking
                             if filename in st.session_state.summary_results and st.session_state.summary_results[filename]['status'] == 'waiting':
                                  st.session_state.summary_results[filename].update({
                                      'status': 'chunking_error',
                                      'error': 'Could not create text chunks',
                                      'success': False
                                  })
                                  st.warning(f"Chunking failed for {filename}.")


                    if not final_summarizable_results:
                         st.error("No usable content chunks were created from the extracted text. Summarization cannot proceed.")
                         logger.error("No usable content chunks created.")
                         st.session_state.summarizing = False # Reset flag
                         st.rerun()
                         # st.stop()


                    # --- Submit Individual Summarization Tasks to Background ---
                    logger.info(f"Submitting {len(final_summarizable_results)} summarization tasks to ThreadPoolExecutor.")

                    # Submit the synchronous summarization task for each document to the background thread
                    # Pass the queue object to the background function
                    for doc_result in final_summarizable_results:
                         filename = doc_result.get('filename', 'unknown')
                         # Pass necessary data and the queue object to the background task
                         # The summarize_extracted_documents in main.py is expected to handle a list of one document
                         future = st.session_state.executor.submit(
                            summarize_extracted_documents, # The synchronous function to run
                            [doc_result], # Pass a list with a single document result
                            retriever,
                            st.session_state.update_queue # Pass the queue
                         )
                         st.session_state.processing_futures[filename] = future
                         # Update initial status in main thread's session state immediately
                         if filename in st.session_state.summary_results:
                              st.session_state.summary_results[filename]['status'] = 'processing' # Initial status while task is running

                    logger.info(f"Individual summarization tasks submitted to background for {len(st.session_state.processing_futures)} files.")

                    # Rerun immediately to show the spinner and initial state with 'processing' tiles
                    st.rerun()

                except FileNotFoundError as fnf_error:
                    st.error(f"Configuration Error: {fnf_error}. Please check your environment settings.")
                    logger.error(f"Configuration Error during Streamlit process: {fnf_error}", exc_info=True)
                    st.session_state.summarizing = False # Reset flag on error
                    st.session_state.overall_error = str(fnf_error)
                    st.session_state.processing_futures = {} # Clear futures on error
                    st.rerun() # Rerun to show error and reset state
                except Exception as e:
                    st.error(f"An unexpected error occurred during initial processing: {e}")
                    logger.error(f"An unexpected error occurred during initial Streamlit process: {e}", exc_info=True)
                    st.session_state.summarizing = False # Reset flag on error
                    st.session_state.overall_error = str(e)
                    st.session_state.processing_futures = {} # Clear futures on error
                    st.rerun() # Rerun to show error and reset state


    # --- Process Queue and Check Background Task Status ---
    # This block runs on every Streamlit rerun
    # Process any updates from the background queue
    updated_from_queue = False
    try:
        while not st.session_state.update_queue.empty():
            update = st.session_state.update_queue.get_nowait()
            filename = update.get('filename')
            status = update.get('status')
            result_data = update.get('result_data')

            if filename and filename in st.session_state.summary_results:
                logger.debug(f"Main thread processing queue update for {filename}: status={status}")
                st.session_state.summary_results[filename]['status'] = status
                if result_data:
                    # Update other fields if result data is provided (e.g., summary, error, success)
                    st.session_state.summary_results[filename].update(result_data)
                updated_from_queue = True
            st.session_state.update_queue.task_done() # Mark the task as done in the queue

    except queue.Empty:
        # This is expected when the queue is empty
        pass
    except Exception as e:
        logger.error(f"Error processing queue: {e}", exc_info=True)
        st.error(f"An error occurred while processing background updates: {e}")


    # Check completed futures to know when tasks are done and potentially trigger rerun
    completed_futures_filenames = [filename for filename, future in st.session_state.processing_futures.items() if future.done()]

    if completed_futures_filenames:
        logger.debug(f"Found {len(completed_futures_filenames)} completed futures.")
        rerun_needed = False
        for filename in completed_futures_filenames:
            # We don't need to get the result here as the queue callback
            # already put the final status/result into the queue and updated session_state.
            # We just need to know the future is done to remove it.
            try:
                # Optional: Check for exceptions in the future if the queue callback
                # didn't handle all error reporting scenarios.
                # future.result() # This would re-raise exceptions from the background task
                pass
            except Exception as e:
                 logger.error(f"Exception in completed future for {filename} (already reported via queue): {e}", exc_info=True)
                 # The queue processing should have updated the status to 'error' already.

            # Remove the completed future
            if filename in st.session_state.processing_futures:
                del st.session_state.processing_futures[filename]
                rerun_needed = True # Indicate that UI might need update if queue updates didn't trigger it

        # Trigger a rerun if any futures completed or if queue was processed
        if rerun_needed or updated_from_queue:
             logger.info("Completed futures processed or queue updated, triggering rerun.")
             st.rerun()

    # Check if all futures are done and no more are being added
    if not st.session_state.processing_futures and st.session_state.summarizing:
        logger.info("All background tasks completed.")
        st.session_state.summarizing = False # Reset overall summarizing flag
        # Trigger a final rerun to show completion status if not already triggered
        if not updated_from_queue: # Avoid double rerun if queue processing already did it
             st.rerun()


    # --- Display Document Tiles and Summaries ---
    # This section reads from the incrementally updated st.session_state.summary_results.
    # It will update whenever Streamlit reruns.
    if st.session_state.summary_results is not None: # Check if the dictionary exists
        st.subheader("Processing Status and Summaries:")

        if not st.session_state.summary_results and not st.session_state.summarizing and st.session_state.overall_error is None:
            st.info("Upload files and click 'Generate Summaries' to begin.")
        elif st.session_state.summarizing or st.session_state.summary_results:
             # Display an overall status message
             total_files = len(st.session_state.summary_results)
             # Count files that have reached a final state
             completed_count = sum(1 for res in st.session_state.summary_results.values() if res.get('status') in ['completed', 'error', 'skipped', 'extraction_error', 'no_text', 'chunking_error'])
             # Count files that are currently in an intermediate processing state
             processing_count = sum(1 for res in st.session_state.summary_results.values() if res.get('status') in ['waiting', 'queued', 'processing', 'summarizing'])

             if st.session_state.summarizing:
                  st.info(f"Processing... {processing_count} in progress, {completed_count} completed or skipped out of {total_files} files.")
             elif st.session_state.overall_error:
                  st.error(f"An error occurred during initial processing: {st.session_state.overall_error}")
                  st.warning(f"{completed_count} files processed with status out of {total_files} attempted.")
             else:
                  successful_summaries = sum(1 for res in st.session_state.summary_results.values() if res.get('status') == 'completed')
                  st.success(f"Finished processing. {successful_summaries} summaries successfully generated out of {total_files} files.")
                  if completed_count < total_files:
                       st.warning(f"Some files ({total_files - completed_count}) did not complete processing.")


        # Display files as a grid of clickable tiles if there are results
        if st.session_state.summary_results:
            files_per_row = 3
            # Use the keys (filenames) for consistent ordering
            filenames = list(st.session_state.summary_results.keys())
            rows = len(filenames) // files_per_row + (len(filenames) % files_per_row > 0)

            for i in range(rows):
                cols = st.columns(files_per_row)
                for j in range(files_per_row):
                    file_index = i * files_per_row + j
                    if file_index < len(filenames):
                        filename = filenames[file_index]
                        result = st.session_state.summary_results.get(filename, {}) # Get result safely
                        status = result.get('status', 'unknown')
                        is_selected = st.session_state.selected_filename == filename

                        # Determine tile appearance based on status
                        if status == 'completed':
                            button_label = f"塘 {filename} (Completed)"
                            # Use a custom style or expander for completed tiles if needed,
                            # or rely on the selection below to show the summary.
                        elif status in ['error', 'skipped', 'extraction_error', 'no_text', 'chunking_error']:
                            button_label = f"☒ {filename} ({status.replace('_', ' ').title()})" # e.g., "Extraction Error"
                        elif status in ['waiting', 'queued', 'processing', 'summarizing']:
                             button_label = f"時計 {filename} ({status.replace('_', ' ').title()})..."
                        else:
                            button_label = f"ファイル {filename} (Status: {status})"

                        # Use a standard button within the column
                        # Add a unique key based on the filename and whether it's selected
                        button_key = f"tile_button_{filename}_{'selected' if is_selected else 'normal'}"
                        # Use beta_container or just cols[j]
                        with cols[j]:
                            # Make the button disabled if it's in a processing state
                            button_disabled = status in ['waiting', 'queued', 'processing', 'summarizing']
                            if st.button(button_label, key=button_key, use_container_width=True, disabled=button_disabled):
                                # Only allow selecting tiles that have finished processing (completed, error, skipped, etc.)
                                if not button_disabled:
                                    st.session_state.selected_filename = filename
                                    logger.info(f"Selected file: {filename}")
                                else:
                                     # This block is technically unreachable because the button is disabled,
                                     # but good practice to handle.
                                     st.info(f"Cannot select '{filename}' while it is still processing or waiting.")
                                     st.session_state.selected_filename = None # Deselect if a processing tile was somehow clicked


            # Display summary/details of the selected file below the tiles
            if st.session_state.selected_filename:
                st.markdown("---") # Separator
                st.subheader(f"Details for: {st.session_state.selected_filename}")

                selected_result = st.session_state.summary_results.get(st.session_state.selected_filename)

                if selected_result:
                    status = selected_result.get('status', 'unknown')
                    st.markdown(f"**Status:** {status.replace('_', ' ').title()}")

                    if status == 'completed':
                        summary = selected_result.get('summary')
                        if summary:
                            st.markdown("---")
                            st.markdown("### Summary:")
                            st.markdown(summary) # Render markdown summary
                        else:
                            st.info(f"Summary is empty for {st.session_state.selected_filename}.")
                    elif selected_result.get('error'):
                         st.error(f"Error details: {selected_result.get('error')}")
                    elif status == 'skipped':
                         st.info("This file was skipped due to initial processing issues.")
                    elif status == 'no_text':
                         st.info("No text could be extracted from this file.")
                    elif status == 'chunking_error':
                         st.info("Could not create usable text chunks from this file.")
                    else:
                         st.info(f"Details are not available for '{st.session_state.selected_filename}' yet as it has status: {status.replace('_', ' ').title()}")


# --- Message if modules failed to load ---
if not modules_loaded:
     st.error("Application modules failed to load. Please check your environment and project setup.")

# --- Message if API Key is not entered and modules loaded ---
if not st.session_state.api_key_entered and modules_loaded and not st.session_state.summarizing:
    st.info("Enter your Cohere API Key above to unlock the application functionality.")

# --- Keep UI updated by rerunning periodically if processing ---
# This is a simple mechanism to trigger reruns to check background task status and queue.
# Adjust sleep time as needed, shorter times make it more responsive but use more CPU.
# Rerun if there are active processing futures or if the queue is not empty.
if st.session_state.processing_futures or not st.session_state.update_queue.empty():
     logger.debug(f"Active processing futures ({len(st.session_state.processing_futures)}) or queue not empty, sleeping briefly to trigger rerun.")
     time.sleep(0.5) # Sleep for a short duration
     st.rerun() # Rerun the app to check for completed futures and process queue
elif st.session_state.summarizing:
    # If summarizing is true but no futures are active and queue is empty,
    # it means initial steps might be running or processing just finished.
    # Rerun once more to update final status.
     logger.debug("Summarizing is True but no active futures and queue is empty, triggering final rerun.")
     st.session_state.summarizing = False # Ensure flag is reset
     st.rerun()

