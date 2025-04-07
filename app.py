import os
import sqlite3
import random
import csv
import datetime # Needed for debug timestamps
import math # Needed for pagination (math.ceil)
import shutil # Needed for potentially removing the whole uploads folder safely
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, g, abort, jsonify
from werkzeug.utils import secure_filename

# --- Configuration ---
DATABASE = 'database.db'
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'} # Allowed image file types
ITEMS_PER_PAGE = 15 # Pagination limit

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = 'your_very_secret_key_here_please_change' # CHANGE THIS!
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024 # Increased limit for multiple files (e.g., 32MB)

# --- Database Helper Functions ---
# (get_db, close_db, init_db, get_photo remain the same)
def get_db():
    """Opens a new database connection if there is none yet for the current application context."""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    """Closes the database again at the end of the request."""
    if hasattr(g, 'db'):
        g.db.close()

def init_db():
    """Initializes the database schema."""
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
        print(f"Created uploads folder: {UPLOAD_FOLDER}")
    with app.app_context():
        db = get_db()
        try:
            with app.open_resource('schema.sql', mode='r') as f:
                db.cursor().executescript(f.read())
            db.commit()
            print("Database schema initialized!")
        except Exception as e:
            print(f"Error initializing database schema: {e}")

def get_photo(photo_id):
    """Gets photo data by its ID."""
    db = get_db()
    photo = db.execute(
        'SELECT id, filename, caption FROM photos WHERE id = ?', (photo_id,)
    ).fetchone()
    if photo is None:
        abort(404, f"Photo id {photo_id} doesn't exist.")
    return photo

# --- Helper Function for File Uploads ---
# (allowed_file remains the same)
def allowed_file(filename):
    """Checks if the filename has an allowed extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Flask CLI Commands ---
# (init_db_command, seed_db_command remain the same)
@app.cli.command('init-db')
def init_db_command():
    """Clear existing data and create new tables."""
    if os.path.exists(DATABASE):
         print(f"WARNING: Database '{DATABASE}' already exists. Re-initializing will delete all data.")
    init_db()
    # Optional: Add a prompt here asking if user wants to delete existing uploads folder content during init

@app.cli.command('seed-db')
def seed_db_command():
    """Seeds the database from photo_data.csv."""
    csv_filepath = 'photo_data.csv'
    if not os.path.exists(csv_filepath):
        print(f"Error: '{csv_filepath}' not found. Please create it.")
        return
    added_count = 0
    skipped_count = 0
    missing_file_count = 0
    try:
        with open(csv_filepath, mode='r', encoding='utf-8') as infile:
            reader = csv.DictReader(infile)
            with app.app_context():
                db = get_db()
                cursor = db.cursor()
                processed_in_seed = set()
                for row in reader:
                    filename = row.get('filename', '').strip()
                    caption = row.get('caption', '').strip()
                    if not filename or not caption:
                        print(f"Warning: Skipping row with missing filename or caption: {row}")
                        skipped_count += 1
                        continue
                    secure_name = secure_filename(filename)
                    if secure_name != filename:
                         print(f"Warning: Filename '{filename}' sanitized to '{secure_name}'. Using original for lookup, secure for DB.")
                         filename_to_store = secure_name # Store secure name
                    else:
                         filename_to_store = filename

                    image_path_original = os.path.join(app.config['UPLOAD_FOLDER'], filename) # Check original first
                    image_path_secure = os.path.join(app.config['UPLOAD_FOLDER'], secure_name)
                    actual_image_path = None

                    if os.path.exists(image_path_secure):
                        actual_image_path = image_path_secure
                    elif os.path.exists(image_path_original):
                        actual_image_path = image_path_original
                        print(f"Info: Found original filename '{filename}' in uploads, will use secure name '{secure_name}' in DB.")
                    else:
                        print(f"Warning: Image file not found in uploads folder (checked '{filename}' and '{secure_name}'). Skipping database entry.")
                        missing_file_count += 1
                        continue

                    # Prevent duplicate filenames *within the CSV processing*
                    if filename_to_store in processed_in_seed:
                         print(f"Info: Filename '{filename_to_store}' already processed in this seed run. Skipping.")
                         skipped_count +=1
                         continue

                    existing = cursor.execute('SELECT id FROM photos WHERE filename = ?', (filename_to_store,)).fetchone()
                    if existing:
                        print(f"Info: Filename '{filename_to_store}' already exists in DB. Skipping.")
                        skipped_count += 1
                        continue
                    try:
                        cursor.execute('INSERT INTO photos (filename, caption) VALUES (?, ?)',
                                       (filename_to_store, caption))
                        added_count += 1
                        processed_in_seed.add(filename_to_store) # Mark as processed
                    except Exception as insert_e:
                        print(f"Error inserting row {row}: {insert_e}")
                        skipped_count += 1
                db.commit()
        print("-" * 20)
        print("Seeding complete!")
        print(f"Added: {added_count} new photo entries.")
        print(f"Skipped: {skipped_count}")
        print(f"Missing image files: {missing_file_count}")
        print("-" * 20)
    except FileNotFoundError:
        print(f"Error: '{csv_filepath}' not found.")
    except Exception as e:
        print(f"An error occurred during seeding: {e}")


# --- Routes ---

# --- NEW: Main Index/Homepage Route ---
@app.route('/')
def index_page():
    """Displays the main introduction/navigation page."""
    return render_template('index.html')
# --- END NEW ROUTE ---

# --- UPDATED: Upload Page Route (Moved from / to /upload) ---
@app.route('/upload', methods=['GET', 'POST']) # <<< URL CHANGED
def upload_page():
    """Handles single or multiple photo uploads via web form."""
    if request.method == 'POST':
        uploaded_files = request.files.getlist('photos') # Use getlist for multiple files
        captions = request.form.getlist('captions')     # Use getlist for multiple captions

        if not uploaded_files or uploaded_files[0].filename == '':
            flash('No photos selected!', 'error')
            return redirect(request.url)

        if len(uploaded_files) != len(captions):
            flash('Mismatch between number of files and captions provided.', 'error')
            print(f"Error: File count ({len(uploaded_files)}) != Caption count ({len(captions)})")
            return redirect(request.url)

        success_count = 0
        error_messages = []
        processed_filenames_in_batch = set()

        db = get_db()

        for i, file in enumerate(uploaded_files):
            caption = captions[i].strip()

            if file.filename == '': continue
            if not caption:
                error_messages.append(f'Caption missing for file "{file.filename}".')
                continue
            if not allowed_file(file.filename):
                error_messages.append(f'Invalid file type for "{file.filename}".')
                continue

            original_filename = file.filename
            filename = secure_filename(original_filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            print(f"\nProcessing file {i+1}: '{original_filename}' -> Secure: '{filename}'")

            if filename in processed_filenames_in_batch:
                 error_messages.append(f'Duplicate filename "{filename}" in this batch.')
                 continue
            existing_db = db.execute('SELECT id FROM photos WHERE filename = ?', (filename,)).fetchone()
            if existing_db:
                error_messages.append(f'Filename "{filename}" already exists in DB.')
                continue

            try:
                print(f"  Attempting save: {filepath}")
                file.save(filepath)
                if not os.path.exists(filepath): raise IOError("File save failed silently.")
                print(f"  File saved.")
                print(f"  Attempting DB insert...")
                cursor = db.execute('INSERT INTO photos (filename, caption) VALUES (?, ?)', (filename, caption))
                db.commit()
                print(f"  DB insert successful. Row ID: {cursor.lastrowid}")
                processed_filenames_in_batch.add(filename)
                success_count += 1
            except Exception as e:
                import traceback
                print(f"---!!! EXCEPTION processing '{filename}' !!!---")
                print(f"  Error: {e}")
                traceback.print_exc()
                error_messages.append(f'Server error uploading "{filename}": {e}')
                # No cleanup attempted here for simplicity, could be added
                print(f"--- END EXCEPTION ---")

        if success_count > 0:
            flash(f'{success_count} photo(s) uploaded successfully!', 'success')
        if error_messages:
            flash('Some photos could not be uploaded:', 'error')
            for msg in error_messages:
                flash(msg, 'error_detail')

        # Redirect back to the upload page itself after processing
        return redirect(url_for('upload_page'))

    return render_template('upload.html')
# --- END OF UPDATED UPLOAD ROUTE ---


# (random_display_page remains the same)
@app.route('/random')
def random_display_page():
    """Displays a single random photo and caption."""
    db = get_db()
    photo_data = db.execute('SELECT filename, caption FROM photos ORDER BY RANDOM() LIMIT 1').fetchone()
    if photo_data:
        return render_template('display.html', photo=photo_data)
    else:
        flash('No photos found in the database.', 'info')
        return render_template('display.html', photo=None)


# (uploaded_file remains the same)
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves the uploaded files from the UPLOAD_FOLDER."""
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        abort(400, "Invalid filename requested.")
    try:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=False)
    except FileNotFoundError:
        abort(404, f"File '{filename}' not found.")
    except Exception as e:
        print(f"Error serving file {filename}: {e}")
        abort(500, "Server error while retrieving file.")


# (manage_photos with pagination remains the same)
@app.route('/manage')
def manage_photos():
    """Displays a paginated list of all photos for management."""
    try: page = int(request.args.get('page', 1))
    except ValueError: page = 1
    if page < 1: page = 1
    limit = ITEMS_PER_PAGE
    offset = (page - 1) * limit
    db = get_db()
    total_items, total_pages, photos_on_page = 0, 0, []
    try:
        total_items = db.execute('SELECT COUNT(id) FROM photos').fetchone()[0]
        if total_items > 0: total_pages = math.ceil(total_items / limit)
        if page > total_pages and total_pages > 0: return redirect(url_for('manage_photos', page=total_pages))
        order_clause = 'ORDER BY uploaded_at DESC'
        try:
            photos_on_page = db.execute(f'SELECT id, filename, caption, uploaded_at FROM photos {order_clause} LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        except sqlite3.OperationalError as e:
             if 'no such column: uploaded_at' in str(e):
                 order_clause = 'ORDER BY id DESC'
                 photos_on_page = db.execute(f'SELECT id, filename, caption FROM photos {order_clause} LIMIT ? OFFSET ?', (limit, offset)).fetchall()
             else: raise e
    except Exception as e:
        print(f"Error retrieving photos for manage page: {e}")
        flash("Error loading photos for management.", "error")
    return render_template('manage.html', photos=photos_on_page, current_page=page, total_pages=total_pages)


# (edit_photo remains the same)
@app.route('/edit/<int:photo_id>', methods=['GET', 'POST'])
def edit_photo(photo_id):
    """Handles editing the caption of a specific photo."""
    photo = get_photo(photo_id)
    if request.method == 'POST':
        new_caption = request.form.get('caption', '').strip()
        if not new_caption:
            flash('Caption cannot be empty!', 'error')
            return render_template('edit.html', photo=photo)
        else:
            try:
                db = get_db()
                db.execute('UPDATE photos SET caption = ? WHERE id = ?', (new_caption, photo_id))
                db.commit()
                flash('Caption updated successfully!', 'success')
                return redirect(url_for('manage_photos', page=request.args.get('page', 1))) # Try to redirect back to original page
            except Exception as e:
                 print(f"Error updating caption for photo ID {photo_id}: {e}")
                 flash(f'Error updating caption: {e}', 'error')
                 return render_template('edit.html', photo=photo)
    return render_template('edit.html', photo=photo)


# (delete_photo remains the same, redirects to page 1)
@app.route('/delete/<int:photo_id>', methods=['POST'])
def delete_photo(photo_id):
    """Handles deleting a single photo entry and its associated file."""
    photo = get_photo(photo_id)
    filename = photo['filename']
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    redirect_url = url_for('manage_photos', page=1) # Simple redirect to page 1
    try:
        db = get_db()
        db.execute('DELETE FROM photos WHERE id = ?', (photo_id,))
        db.commit()
        print(f"Deleted record ID {photo_id} (Filename: {filename}) from DB.")
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                print(f"Deleted file: {filepath}")
                flash(f'Photo "{filename}" deleted successfully!', 'success')
            except OSError as e:
                print(f"Error deleting file {filepath}: {e}")
                flash(f'DB entry deleted, but failed to delete file "{filename}".', 'error')
        else:
            print(f"File was already missing: {filepath}")
            flash(f'DB entry deleted, but file "{filename}" was missing.', 'warning')
    except Exception as e:
        print(f"Error deleting photo record ID {photo_id}: {e}")
        flash(f'Error deleting photo DB entry: {e}', 'error')
    return redirect(redirect_url)

# --- NEW: Delete All Photos Route ---
@app.route('/delete_all', methods=['POST'])
def delete_all_photos():
    """Deletes ALL photos from the database and the uploads folder."""
    print("\n---!!! DELETE ALL PHOTOS INITIATED !!!---")
    db = get_db()
    deleted_db_count = 0
    deleted_file_count = 0
    error_files = []

    try:
        # 1. Delete all records from the database
        print("Attempting to delete all records from 'photos' table...")
        cursor = db.execute('DELETE FROM photos')
        deleted_db_count = cursor.rowcount # Get affected row count
        db.commit()
        print(f"Successfully deleted {deleted_db_count} records from the database.")

        # 2. Delete all files from the uploads folder
        print(f"Attempting to delete all files from folder: {app.config['UPLOAD_FOLDER']}")
        if not os.path.exists(app.config['UPLOAD_FOLDER']):
             print("Uploads folder does not exist. Nothing to delete.")
        else:
            for filename in os.listdir(app.config['UPLOAD_FOLDER']):
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                try:
                    # Make sure it's a file, not a subdirectory (though unlikely here)
                    if os.path.isfile(filepath) or os.path.islink(filepath):
                        os.remove(filepath)
                        print(f"  Deleted file: {filename}")
                        deleted_file_count += 1
                    # elif os.path.isdir(filepath): # Optional: handle subdirs if needed
                    #     shutil.rmtree(filepath) # Use shutil.rmtree for directories
                except Exception as e:
                    print(f"  ERROR deleting file {filename}: {e}")
                    error_files.append(filename)

        # 3. Flash results
        flash(f'Successfully deleted {deleted_db_count} database entries and {deleted_file_count} files.', 'success')
        if error_files:
            flash(f'Could not delete {len(error_files)} file(s): {", ".join(error_files)}', 'error')
        print("--- DELETE ALL PHOTOS COMPLETED ---")

    except Exception as e:
        import traceback
        print("---!!! CRITICAL ERROR DURING DELETE ALL !!!---")
        print(f"Error: {e}")
        traceback.print_exc()
        flash(f'A critical error occurred during deletion: {e}', 'error')
        # Attempt rollback if DB deletion was partial? (Less likely with simple DELETE)
        # db.rollback()
        print("--- END DELETE ALL ERROR ---")

    # Redirect to the main index page after deletion
    return redirect(url_for('index_page'))
# --- END DELETE ALL ROUTE ---


# --- Main Execution ---
if __name__ == '__main__':
    if not os.path.exists(DATABASE):
        print(f"Database '{DATABASE}' not found. Run 'flask init-db'.")
    if not os.path.exists(UPLOAD_FOLDER):
        print(f"Upload folder '{UPLOAD_FOLDER}' not found. Creating it.")
        os.makedirs(UPLOAD_FOLDER)
    app.run(debug=True) #change on upload
