# app.py (With Conflict Renaming Logic)

import os
import sqlite3
import random
import math
import shutil
import secrets # Needed for renaming
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, send_from_directory, g, abort, jsonify,
                   session)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import secrets # Needed for generating random hex for renaming
from werkzeug.exceptions import RequestEntityTooLarge

# --- Constants and Configuration ---
DATABASE = 'database.db'
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
ITEMS_PER_PAGE = 15
ALBUMS_PER_PAGE = 10
SECRET_KEY_FOR_FLASK = 'BZD:p8CEiu*o%k@Y!dYJN1xF!m:+5d' # <<< MUST BE STRONG AND SECRET
DEFAULT_ALBUM_TITLE = 'General Photos'
DEFAULT_COVER_FILENAME = 'default_cover.png'
MAX_TOTAL_STORAGE_BYTES = 500 * 1024 * 1024

# --- Create Flask App Instance ---
app = Flask(__name__)

# --- Apply Configuration to the App Instance ---
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = SECRET_KEY_FOR_FLASK
app.config['MAX_CONTENT_LENGTH'] = 128 * 1024 * 1024

# --- Database Helper Functions ---
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    if hasattr(g, 'db'):
        g.db.close()

def init_db():
    """Initializes the database schema AND clears the uploads folder (except default cover)."""
    uploads_path = app.config['UPLOAD_FOLDER']

    # --- 1. Clear Uploads Folder (Except Default Cover) ---
    if os.path.exists(uploads_path):
        print(f"Clearing contents of folder: {uploads_path} (excluding {DEFAULT_COVER_FILENAME})")
        for filename in os.listdir(uploads_path):
            if filename == DEFAULT_COVER_FILENAME:
                print(f"  Skipping deletion of {DEFAULT_COVER_FILENAME}")
                continue

            filepath = os.path.join(uploads_path, filename)
            try:
                if os.path.isfile(filepath) or os.path.islink(filepath): os.unlink(filepath)
                elif os.path.isdir(filepath): shutil.rmtree(filepath)
            except Exception as e: print(f'  Failed to delete {filepath}. Reason: {e}')
    else:
        os.makedirs(uploads_path); print(f"Created uploads folder: {uploads_path}")

    # Ensure default cover exists AFTER potential cleanup/creation
    default_cover_path = os.path.join(uploads_path, DEFAULT_COVER_FILENAME)
    if not os.path.exists(default_cover_path): print(f"WARNING: Default cover '{DEFAULT_COVER_FILENAME}' not found in '{uploads_path}' after cleanup.")

    # --- 2. Initialize Database Schema ---
    with app.app_context():
        db = get_db()
        try:
            schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
            if not os.path.exists(schema_path): raise FileNotFoundError(f"schema.sql not found at {schema_path}")
            with open(schema_path, mode='r') as f: db.cursor().executescript(f.read())
            db.commit(); print("Database schema initialized!")
        except Exception as e: print(f"Error initializing database schema: {e}")

# --- Album & Photo Helpers ---
def get_album(album_id, check_exists=True):
    db = get_db()
    album = db.execute('SELECT id, title, description, cover_filename, password_hash FROM albums WHERE id = ?', (album_id,)).fetchone()
    if album is None and check_exists: abort(404, f"Album id {album_id} doesn't exist.")
    return album

def get_photo(photo_id, album_id=None, check_exists=True):
    db = get_db()
    query = 'SELECT id, filename, caption, album_id FROM photos WHERE id = ?'
    params = (photo_id,)
    if album_id is not None: query += ' AND album_id = ?'; params += (album_id,)
    photo = db.execute(query, params).fetchone()
    if photo is None and check_exists: abort(404, f"Photo id {photo_id} doesn't exist" + (f" in album {album_id}." if album_id else "."))
    if album_id is not None and photo and photo['album_id'] != album_id and check_exists: abort(403, f"Photo id {photo_id} does not belong to album id {album_id}.")
    return photo

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Flask CLI Commands ---
@app.cli.command('init-db')
def init_db_command():
    if os.path.exists(DATABASE): print(f"WARNING: Re-initializing will delete {DATABASE}.")
    init_db()

# --- Access Control Decorator ---
def album_access_required(f):
    @wraps(f)
    def decorated_function(album_id, *args, **kwargs):
        album = get_album(album_id)
        if album['password_hash']:
            if 'authorized_album_ids' not in session: session['authorized_album_ids'] = []
            if album_id not in session['authorized_album_ids']:
                flash(f"Password required to access album '{album['title']}'.", 'info')
                return redirect(url_for('authorize_album', album_id=album_id))
        return f(album_id=album_id, album=album, *args, **kwargs)
    return decorated_function

# --- Routes ---

@app.route('/')
def introduction():
    """Displays the main introduction/landing page."""
    return render_template('intro.html', album=None)

# app.py - Add this helper function

def get_current_upload_size():
    """Calculates the total size of files currently in the UPLOAD_FOLDER."""
    total_size = 0
    upload_path = app.config['UPLOAD_FOLDER']
    try:
        if os.path.exists(upload_path):
            for item in os.listdir(upload_path):
                item_path = os.path.join(upload_path, item)
                if os.path.isfile(item_path):
                    try:
                        total_size += os.path.getsize(item_path)
                    except OSError as e:
                        print(f"Warning: Could not get size of {item_path}: {e}")
    except Exception as e:
        print(f"Error calculating upload size: {e}")
        # Return a large value on error to potentially prevent uploads? Or 0?
        # Let's return 0 and log the error, assuming it's temporary.
        return 0
    return total_size

@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(e):
    """Custom handler for 413 Request Entity Too Large error."""
    print(f"Intercepted RequestEntityTooLarge error: {e}") # Log the error details server-side

    # Determine where to redirect based on the request path that failed
    # This helps send the user back to the correct upload form
    failed_path = request.path
    redirect_url = url_for('list_albums') # Default redirect
    album_id_for_redirect = None

    # Try to extract album_id if it was an album photo upload
    # Example path: /albums/5/upload
    path_parts = failed_path.strip('/').split('/')
    if len(path_parts) >= 3 and path_parts[0] == 'albums' and path_parts[2] == 'upload':
        try:
            album_id_for_redirect = int(path_parts[1])
            redirect_url = url_for('upload_photo_to_album', album_id=album_id_for_redirect)
        except (ValueError, IndexError):
            # Couldn't parse album_id, use default redirect
            pass
    elif failed_path == url_for('create_album'): # Check if it was the create album cover upload
         redirect_url = url_for('create_album')
    # Add elif for edit_album cover if necessary
    elif len(path_parts) >= 3 and path_parts[0] == 'albums' and path_parts[2] == 'edit':
         try:
             album_id_for_redirect = int(path_parts[1])
             redirect_url = url_for('edit_album', album_id=album_id_for_redirect)
         except (ValueError, IndexError):
             pass


    # Flash a user-friendly message
    flash(f"Upload failed: The total size of the selected files exceeds the limit ({app.config['MAX_CONTENT_LENGTH'] // (1024*1024)} MB). Please select fewer files or smaller images.", 'error')

    # Redirect the user back to the appropriate form
    return redirect(redirect_url)

@app.route('/albums')
def list_albums():
    """Displays the list of albums with pagination, ensuring default is first."""
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    if page < 1: page = 1

    limit = ALBUMS_PER_PAGE
    offset = (page - 1) * limit
    db = get_db()
    albums_on_page = []
    total_items = 0
    total_pages = 0

    try:
        # Count total albums (doesn't need special order)
        total_items = db.execute('SELECT COUNT(id) FROM albums').fetchone()[0]

        if total_items > 0:
            total_pages = math.ceil(total_items / limit)
            # Redirect if page number is out of bounds
            if page > total_pages and total_pages > 0:
                return redirect(url_for('list_albums', page=total_pages))

            # <<< MODIFIED QUERY: Use CASE for sorting >>>
            # Order by 0 if it's the default album, 1 otherwise, then by title
            query = """
                SELECT id, title, description, cover_filename, password_hash
                FROM albums
                ORDER BY
                    CASE WHEN title = ? THEN 0 ELSE 1 END,
                    title ASC
                LIMIT ? OFFSET ?
            """
            albums_on_page = db.execute(query, (DEFAULT_ALBUM_TITLE, limit, offset)).fetchall()
            # <<< END MODIFIED QUERY >>>

    except Exception as e:
        print(f"Error fetching albums: {e}")
        flash("Could not retrieve albums.", "error")

    # Pass DEFAULT_ALBUM_TITLE to template for conditional logic (like the delete button)
    return render_template('index.html', albums=albums_on_page, current_page=page, total_pages=total_pages, album=None, DEFAULT_ALBUM_TITLE=DEFAULT_ALBUM_TITLE)

# app.py - Modify create_album

@app.route('/albums/new', methods=['GET', 'POST'])
def create_album():
    """Handles creating a new album, checking total storage limit for cover."""
    if request.method == 'POST':
        # --- Get Form Data ---
        # ... (title, description, password, etc) ...
        cover_file = request.files.get('cover')

        # --- Basic Validation ---
        errors = []
        # ... (title, file type, password validation) ...
        incoming_cover_size = 0
        if cover_file and cover_file.filename != '' and allowed_file(cover_file.filename):
            try:
                original_pos = cover_file.tell()
                cover_file.seek(0, os.SEEK_END)
                incoming_cover_size = cover_file.tell()
                cover_file.seek(original_pos) # Reset pointer
            except Exception as e:
                print(f"Error getting cover size: {e}")
                errors.append("Could not determine size of cover image.")
        elif cover_file is None or cover_file.filename == '':
             errors.append('Album cover photo is required.') # Already checked but repeat for clarity

        if errors:
            for error in errors: flash(error, 'error')
            return render_template('create_album.html', title=request.form.get('title', ''), description=request.form.get('description', ''), album=None)

        # <<< START STORAGE LIMIT CHECK >>>
        current_disk_usage = get_current_upload_size()
        print(f"Current usage: {current_disk_usage} bytes. Incoming cover: {incoming_cover_size} bytes. Limit: {MAX_TOTAL_STORAGE_BYTES} bytes.")
        if current_disk_usage + incoming_cover_size > MAX_TOTAL_STORAGE_BYTES:
             available_space_mb = (MAX_TOTAL_STORAGE_BYTES - current_disk_usage) // (1024 * 1024)
             flash(f"Cannot create album: Adding the cover photo would exceed the total storage limit ({MAX_TOTAL_STORAGE_BYTES // (1024*1024)} MB).", 'error')
             flash(f"Approximately {available_space_mb} MB remaining.", 'error')
             return render_template('create_album.html', title=request.form.get('title', ''), description=request.form.get('description', ''), album=None)
        # <<< END STORAGE LIMIT CHECK >>>

        # --- Check Title Uniqueness ---
        # ... (db check for title) ...

        # --- Check Cover Filename Conflict & Rename ---
        # ... (conflict check and rename logic using secrets.token_hex) ...
        # ... Make sure this uses the cover_file object ...
        original_cover_filename = cover_file.filename
        filename = secure_filename(original_cover_filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        renamed_cover = None
        conflict = False
        if os.path.exists(filepath) or \
           db.execute('SELECT 1 FROM photos WHERE filename = ?', (filename,)).fetchone() or \
           db.execute('SELECT 1 FROM albums WHERE cover_filename = ?', (filename,)).fetchone(): conflict = True
        if conflict:
            was_renamed = False; attempts = 0; max_attempts = 5; base, ext = os.path.splitext(filename)
            while conflict and attempts < max_attempts:
                 attempts += 1; suffix = secrets.token_hex(3); new_filename_attempt = f"{base}_{suffix}{ext}"
                 new_filepath_attempt = os.path.join(app.config['UPLOAD_FOLDER'], new_filename_attempt)
                 conflict = os.path.exists(new_filepath_attempt) or \
                            db.execute('SELECT 1 FROM photos WHERE filename = ?', (new_filename_attempt,)).fetchone() or \
                            db.execute('SELECT 1 FROM albums WHERE cover_filename = ?', (new_filename_attempt,)).fetchone()
                 if not conflict: renamed_cover = (original_cover_filename, new_filename_attempt); filename = new_filename_attempt; filepath = new_filepath_attempt; was_renamed = True; print(f"Cover conflict for '{original_cover_filename}', renamed to '{filename}'"); break
            if not was_renamed: flash(f"Cover photo filename '{original_cover_filename}' conflicts, and renaming failed.", 'error'); return render_template('create_album.html', title=title, description=description, album=None)

        # --- Process Creation ---
        try:
            cover_file.seek(0) # Reset pointer before saving
            cover_file.save(filepath)
            if not os.path.exists(filepath): raise IOError("Cover file save failed")
            cursor = db.execute('INSERT INTO albums (title, description, cover_filename, password_hash) VALUES (?, ?, ?, ?)', (title, description, filename, hashed_pw))
            db.commit(); flash(f"Album '{title}' created successfully!", 'success')
            if renamed_cover: flash(f"Note: Cover photo '{renamed_cover[0]}' was renamed to '{renamed_cover[1]}'.", 'info_detail')
            return redirect(url_for('list_albums'))
        except Exception as e:
            print(f"Error creating album: {e}"); flash(f"Error creating album: {e}", 'error')
            if os.path.exists(filepath):
            # Indent this block
            try:
                os.remove(filepath) # Or os.unlink()
                print(f"  Cleaned up cover file after error: {filepath}")
            except OSError as cleanup_e:
                print(f"  Error during cover file cleanup: {cleanup_e}")
                pass # Ignore cleanup error
        #    Ensure this return is outside the 'if os.path.exists...' but inside the except
            return render_template('create_album.html', title=title, description=description, album=None)

    # GET request
    return render_template('create_album.html', album=None)


@app.route('/albums/<int:album_id>/authorize', methods=['GET', 'POST'])
def authorize_album(album_id):
    """Handles password entry for protected albums."""
    album = get_album(album_id)
    if not album['password_hash']: return redirect(url_for('album_home', album_id=album_id))

    if request.method == 'POST':
        provided_password = request.form.get('password')
        if check_password_hash(album['password_hash'], provided_password):
            if 'authorized_album_ids' not in session: session['authorized_album_ids'] = []
            authorized_ids = session['authorized_album_ids']
            if album_id not in authorized_ids: authorized_ids.append(album_id)
            session['authorized_album_ids'] = authorized_ids
            session.modified = True
            flash(f"Access granted to album '{album['title']}'.", 'success')
            return redirect(url_for('album_home', album_id=album_id))
        else:
            flash('Incorrect password.', 'error')
            return redirect(url_for('authorize_album', album_id=album_id))

    # GET request - Pass original album for display, None for nav context
    return render_template('authorize_album.html', album_for_form=album, album=None)

@app.route('/albums/<int:album_id>')
@album_access_required
def album_home(album_id, album):
    return render_template('album_home.html', album=album)

# app.py - Modify upload_photo_to_album

@app.route('/albums/<int:album_id>/upload', methods=['GET', 'POST'])
@album_access_required
def upload_photo_to_album(album_id, album):
    """Handles uploading photos, checking total storage limit first."""
    if request.method == 'POST':
        uploaded_files = request.files.getlist('photos')
        captions = request.form.getlist('captions') # Captions can be empty

        if not uploaded_files or uploaded_files[0].filename == '':
            flash('No photos selected!', 'error'); return redirect(url_for('upload_photo_to_album', album_id=album_id))
        if len(uploaded_files) != len(captions):
            flash('Mismatch between files and captions provided.', 'error'); return redirect(url_for('upload_photo_to_album', album_id=album_id))

        # <<< START STORAGE LIMIT CHECK >>>
        current_disk_usage = get_current_upload_size()
        incoming_batch_size = 0
        valid_files_in_request = [] # Store valid files to process later

        # Calculate total size of valid incoming files
        for i, file in enumerate(uploaded_files):
            if file and file.filename != '' and allowed_file(file.filename):
                 # Get file size without saving permanently
                 try:
                     # Save current position, seek to end, get size, reset position
                     original_pos = file.tell()
                     file.seek(0, os.SEEK_END)
                     incoming_batch_size += file.tell()
                     file.seek(original_pos) # IMPORTANT: Reset stream pointer
                     valid_files_in_request.append({'file_obj': file, 'caption': captions[i].strip(), 'original_filename': file.filename})
                 except Exception as e:
                      print(f"Error getting size for {file.filename}: {e}")
                      flash(f"Could not determine size for {file.filename}, skipping.", "warning")

        print(f"Current usage: {current_disk_usage} bytes. Incoming batch: {incoming_batch_size} bytes. Limit: {MAX_TOTAL_STORAGE_BYTES} bytes.")

        # Check if adding the incoming batch exceeds the total limit
        if current_disk_usage + incoming_batch_size > MAX_TOTAL_STORAGE_BYTES:
            available_space_mb = (MAX_TOTAL_STORAGE_BYTES - current_disk_usage) // (1024 * 1024)
            flash(f"Upload failed: Adding these files would exceed the total storage limit ({MAX_TOTAL_STORAGE_BYTES // (1024*1024)} MB).", 'error')
            flash(f"Approximately {available_space_mb} MB remaining. Please upload smaller files or fewer files.", 'error')
            return redirect(url_for('upload_photo_to_album', album_id=album_id))
        # <<< END STORAGE LIMIT CHECK >>>


        # --- Process only the valid files identified earlier ---
        success_count = 0
        skipped_batch_duplicates = []
        renamed_files = []
        error_messages = []
        processed_filenames_in_batch = set()
        db = get_db()

        for file_data in valid_files_in_request: # Iterate through validated files
            file = file_data['file_obj']
            caption = file_data['caption']
            original_filename = file_data['original_filename']

            # (Rest of the existing logic: secure_filename, check batch duplicates, check conflicts, rename, save, db insert)
            # ... Make sure to use 'file' (the object) and 'caption' from file_data ...
            filename = secure_filename(original_filename)
            if filename in processed_filenames_in_batch: skipped_batch_duplicates.append(original_filename); continue
            processed_filenames_in_batch.add(filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            conflict = False
            if os.path.exists(filepath) or \
               db.execute('SELECT 1 FROM photos WHERE filename = ?', (filename,)).fetchone() or \
               db.execute('SELECT 1 FROM albums WHERE cover_filename = ?', (filename,)).fetchone():
                conflict = True
            if conflict:
                was_renamed = False; attempts = 0; max_attempts = 5
                base, ext = os.path.splitext(filename)
                while conflict and attempts < max_attempts:
                    attempts += 1; suffix = secrets.token_hex(3)
                    new_filename_attempt = f"{base}_{suffix}{ext}"
                    new_filepath_attempt = os.path.join(app.config['UPLOAD_FOLDER'], new_filename_attempt)
                    conflict = os.path.exists(new_filepath_attempt) or \
                               db.execute('SELECT 1 FROM photos WHERE filename = ?', (new_filename_attempt,)).fetchone() or \
                               db.execute('SELECT 1 FROM albums WHERE cover_filename = ?', (new_filename_attempt,)).fetchone()
                    if not conflict:
                        renamed_files.append((original_filename, new_filename_attempt))
                        filename = new_filename_attempt; filepath = new_filepath_attempt
                        was_renamed = True; print(f"Conflict for '{original_filename}', renamed to '{filename}'"); break
                if not was_renamed: error_messages.append(f'Conflict error for "{original_filename}".'); continue
            try:
                file.seek(0) # Ensure pointer is at start before saving
                file.save(filepath)
                if not os.path.exists(filepath): raise IOError("File save failed silently.")
                cursor = db.execute('INSERT INTO photos (album_id, filename, caption) VALUES (?, ?, ?)', (album_id, filename, caption if caption else None)); db.commit(); success_count += 1
            except Exception as e:
                print(f"EXC upload {original_filename} (final name: {filename}): {e}"); error_messages.append(f'Server error uploading "{original_filename}".')
                if os.path.exists(filepath): try: os.remove(filepath); print(f"Cleaned up failed upload: {filepath}"); except OSError: pass
        # --- End loop ---

        # (Flash messages remain the same)
        if success_count > 0: flash(f'{success_count} photo(s) uploaded to "{album["title"]}"!', 'success')
        if renamed_files: flash("Some filenames renamed due to conflicts:", 'info'); [flash(f"- '{o}' → '{n}'", 'info_detail') for o, n in renamed_files]
        if skipped_batch_duplicates: flash("Skipped duplicates within batch:", 'warning'); [flash(f"- '{s}'", 'warning_detail') for s in skipped_batch_duplicates]
        if error_messages: flash(f'Upload errors for "{album["title"]}":', 'error'); [flash(msg, 'error_detail') for msg in error_messages]
        return redirect(url_for('upload_photo_to_album', album_id=album_id))

    # GET request
    return render_template('upload.html', album=album)

@app.route('/albums/<int:album_id>/random')
@album_access_required
def random_photo_in_album(album_id, album):
    db = get_db()
    photo_data = db.execute('SELECT filename, caption FROM photos WHERE album_id = ? ORDER BY RANDOM() LIMIT 1', (album_id,)).fetchone()
    if photo_data:
        return render_template('display.html', photo=photo_data, album=album)
    else:
        flash(f'No photos found in album "{album["title"]}".', 'info')
        return render_template('display.html', photo=None, album=album)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename: abort(400)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
    if not os.path.isfile(filepath): abort(404)
    try: return send_from_directory(app.config['UPLOAD_FOLDER'], safe_filename, as_attachment=False)
    except Exception as e: print(f"Error serving {filename}: {e}"); abort(500)

@app.route('/albums/<int:album_id>/manage')
@album_access_required
def manage_photos_in_album(album_id, album):
    try: page = int(request.args.get('page', 1))
    except ValueError: page = 1
    if page < 1: page = 1
    limit = ITEMS_PER_PAGE; offset = (page - 1) * limit; db = get_db()
    total_items, total_pages, photos_on_page = 0, 0, []
    try:
        total_items = db.execute('SELECT COUNT(id) FROM photos WHERE album_id = ?', (album_id,)).fetchone()[0]
        if total_items > 0:
            total_pages = math.ceil(total_items / limit)
            if page > total_pages and total_pages > 0: return redirect(url_for('manage_photos_in_album', album_id=album_id, page=total_pages))
            order_clause = 'ORDER BY uploaded_at DESC'; base_query = 'SELECT id, filename, caption, uploaded_at FROM photos WHERE album_id = ?'
            try: photos_on_page = db.execute(f'{base_query} {order_clause} LIMIT ? OFFSET ?', (album_id, limit, offset)).fetchall()
            except sqlite3.OperationalError:
                 order_clause = 'ORDER BY id DESC'; base_query = 'SELECT id, filename, caption FROM photos WHERE album_id = ?'
                 photos_on_page = db.execute(f'{base_query} {order_clause} LIMIT ? OFFSET ?', (album_id, limit, offset)).fetchall()
    except Exception as e: print(f"Err manage photos alb {album_id}: {e}"); flash("Error loading photos.", "error")
    return render_template('manage.html', album=album, photos=photos_on_page, current_page=page, total_pages=total_pages, DEFAULT_ALBUM_TITLE=DEFAULT_ALBUM_TITLE)

# app.py - Modify edit_album

@app.route('/albums/<int:album_id>/edit', methods=['GET', 'POST'])
@album_access_required
def edit_album(album_id, album):
    """Handles editing album details, checking storage limit for new cover."""
    is_default_album = (album['title'] == DEFAULT_ALBUM_TITLE)

    if request.method == 'POST':
        # --- Get Form Data ---
        # ... (title, description, password, etc) ...
        new_cover_file = request.files.get('cover')

        # --- Basic Validation ---
        errors = []
        # ... (title, password validation) ...

        # --- Validate and Process New Cover Image ---
        new_cover_filename = None
        new_cover_filepath = None
        old_cover_filename = album['cover_filename']
        incoming_cover_size = 0
        old_cover_size = 0 # Need size of old cover if replacing

        if new_cover_file and new_cover_file.filename != '':
            if not allowed_file(new_cover_file.filename):
                errors.append('Invalid file type for new cover photo.')
            else:
                # Get incoming size
                try:
                    original_pos = new_cover_file.tell()
                    new_cover_file.seek(0, os.SEEK_END)
                    incoming_cover_size = new_cover_file.tell()
                    new_cover_file.seek(original_pos)
                except Exception as e:
                     print(f"Error getting new cover size: {e}")
                     errors.append("Could not determine size of new cover image.")

                # <<< START STORAGE LIMIT CHECK (only if new cover is uploaded) >>>
                if incoming_cover_size > 0 and not errors: # Proceed only if size known and no prior errors
                     current_disk_usage = get_current_upload_size()
                     # Get size of the file being replaced (if it exists and isn't default)
                     old_cover_filepath = os.path.join(app.config['UPLOAD_FOLDER'], old_cover_filename)
                     if old_cover_filename != DEFAULT_COVER_FILENAME and os.path.isfile(old_cover_filepath):
                         try:
                             old_cover_size = os.path.getsize(old_cover_filepath)
                         except OSError:
                              old_cover_size = 0 # Assume 0 if cannot get size

                     print(f"Current usage: {current_disk_usage}. Old cover: {old_cover_size}. Incoming cover: {incoming_cover_size}. Limit: {MAX_TOTAL_STORAGE_BYTES}.")
                     # Check limit considering the old file will be removed
                     if (current_disk_usage - old_cover_size + incoming_cover_size) > MAX_TOTAL_STORAGE_BYTES:
                          available_space_mb = max(0, (MAX_TOTAL_STORAGE_BYTES - current_disk_usage + old_cover_size)) // (1024 * 1024)
                          errors.append(f"Cannot update cover: Adding the new file would exceed the total storage limit ({MAX_TOTAL_STORAGE_BYTES // (1024*1024)} MB).")
                          errors.append(f"Approximately {available_space_mb} MB available after removing old cover.")
                # <<< END STORAGE LIMIT CHECK >>>

                # --- Conflict Check & Rename (only if limit check passed) ---
                if not errors:
                    # ... (existing conflict check and rename logic for new_cover_filename/filepath) ...
                    # ... This part remains largely the same, just ensure it uses new_cover_file object...
                    original_new_filename = new_cover_file.filename; filename_attempt = secure_filename(original_new_filename); filepath_attempt = os.path.join(app.config['UPLOAD_FOLDER'], filename_attempt); conflict = False
                    if filename_attempt != old_cover_filename:
                        db = get_db(); conflict = os.path.exists(filepath_attempt) or \
                                   db.execute('SELECT 1 FROM photos WHERE filename = ?', (filename_attempt,)).fetchone() or \
                                   db.execute('SELECT 1 FROM albums WHERE cover_filename = ? AND id != ?', (filename_attempt, album_id)).fetchone()
                    if conflict:
                        was_renamed = False; attempts = 0; max_attempts = 5; base, ext = os.path.splitext(filename_attempt)
                        while conflict and attempts < max_attempts:
                            attempts += 1; suffix = secrets.token_hex(3); new_filename_renamed = f"{base}_{suffix}{ext}"; new_filepath_renamed = os.path.join(app.config['UPLOAD_FOLDER'], new_filename_renamed)
                            conflict = os.path.exists(new_filepath_renamed) or \
                                       db.execute('SELECT 1 FROM photos WHERE filename = ?', (new_filename_renamed,)).fetchone() or \
                                       db.execute('SELECT 1 FROM albums WHERE cover_filename = ? AND id != ?', (new_filename_renamed, album_id)).fetchone()
                            if not conflict: renamed_cover = (original_new_filename, new_filename_renamed); new_cover_filename = new_filename_renamed; new_cover_filepath = new_filepath_renamed; was_renamed = True; print(f"New cover conflict for '{original_new_filename}', renamed to '{new_cover_filename}'"); break
                        if not was_renamed: errors.append(f"New cover filename '{original_new_filename}' conflicts, renaming failed."); new_cover_filename = None; new_cover_filepath = None
                    elif filename_attempt: new_cover_filename = filename_attempt; new_cover_filepath = filepath_attempt

        # If a valid new cover is ready, add to update dict
        if new_cover_filename and not errors: # Only add if no errors encountered during cover processing
             update_fields['cover_filename'] = new_cover_filename

        # --- Handle Validation Errors (including storage limit) ---
        if errors:
            for error in errors: flash(error, 'error')
            return render_template('edit_album.html', album=album, current_title=request.form.get('title', album['title']), current_description=request.form.get('description', album['description']))

        # --- Proceed with Update ---
        # ... (rest of the update logic: check if update_fields, save file, update DB, delete old file) ...
        # ... Ensure new_cover_file.seek(0) before saving ...
        if not update_fields: flash("No changes detected.", "info"); return redirect(url_for('album_home', album_id=album_id))
        db = get_db()
        try:
            saved_new_file = False
            if new_cover_filename and new_cover_filepath:
                try: new_cover_file.seek(0); new_cover_file.save(new_cover_filepath); saved_new_file = True; print("New cover saved.")
                except Exception as file_e: raise Exception(f"Failed to save new cover: {file_e}")
            set_clauses = []; params = [];
            for key, value in update_fields.items(): set_clauses.append(f"{key} = ?"); params.append(value)
            params.append(album_id); sql = f"UPDATE albums SET {', '.join(set_clauses)} WHERE id = ?"; db.execute(sql, tuple(params)); db.commit(); print("Album updated.")
            if saved_new_file and old_cover_filename != new_cover_filename and old_cover_filename != DEFAULT_COVER_FILENAME:
                old_cover_filepath = os.path.join(app.config['UPLOAD_FOLDER'], old_cover_filename)
                if os.path.exists(old_cover_filepath): try: os.remove(old_cover_filepath); print(f"Deleted old cover: {old_cover_filepath}")
                except OSError as del_e: print(f"Warn: Failed del old cover {old_cover_filepath}: {del_e}"); flash(f"Failed del old cover '{old_cover_filename}'.", 'warning')
            flash("Album details updated!", 'success')
            if renamed_cover: flash(f"Note: New cover '{renamed_cover[0]}' was renamed to '{renamed_cover[1]}'.", 'info_detail')
            return redirect(url_for('album_home', album_id=album_id))
        except Exception as e:
            db.rollback(); print(f"Error updating album {album_id}: {e}"); flash(f"Error updating album: {e}", 'error')
            if saved_new_file and new_cover_filepath and os.path.exists(new_cover_filepath): try: os.remove(new_cover_filepath); print(f"Cleaned up new cover: {new_cover_filepath}"); except OSError: pass
            return render_template('edit_album.html', album=album, current_title=request.form.get('title', album['title']), current_description=request.form.get('description', album['description']))

    # --- Handle GET Request ---
    return render_template('edit_album.html', album=album, current_title=album['title'], current_description=album['description'])

@app.route('/albums/<int:album_id>/edit/<int:photo_id>', methods=['GET', 'POST'])
@album_access_required
def edit_photo_in_album(album_id, photo_id, album):
    photo = get_photo(photo_id, album_id=album_id)
    if request.method == 'POST':
        new_caption = request.form.get('caption', '').strip()
        try:
            db = get_db(); db.execute('UPDATE photos SET caption = ? WHERE id = ? AND album_id = ?', (new_caption if new_caption else None, photo_id, album_id)); db.commit()
            flash('Caption updated!', 'success')
            return redirect(url_for('manage_photos_in_album', album_id=album_id, page=request.args.get('page', 1)))
        except Exception as e: print(f"Err update caption {photo_id}: {e}"); flash(f'Error updating caption: {e}', 'error'); return render_template('edit.html', photo=photo, album=album)
    return render_template('edit.html', photo=photo, album=album)

@app.route('/albums/<int:album_id>/delete/<int:photo_id>', methods=['POST'])
@album_access_required
def delete_photo_from_album(album_id, photo_id, album):
    photo = get_photo(photo_id, album_id=album_id)
    filename = photo['filename']; filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    redirect_url = url_for('manage_photos_in_album', album_id=album_id, page=request.args.get('page', 1))
    try:
        db = get_db(); db.execute('DELETE FROM photos WHERE id = ? AND album_id = ?', (photo_id, album_id)); db.commit()
        print(f"Deleted record {photo_id} ({filename}).")
        if os.path.exists(filepath):
            try: os.remove(filepath); print(f"Deleted file: {filepath}"); flash(f'"{filename}" deleted!', 'success')
            except OSError as e: print(f"Err del file {filepath}: {e}"); flash(f'DB deleted, failed file delete "{filename}".', 'error')
        else: print(f"File missing: {filepath}"); flash(f'DB deleted, file "{filename}" missing.', 'warning')
    except Exception as e: print(f"Err del photo {photo_id}: {e}"); flash(f'Error deleting DB entry: {e}', 'error')
    return redirect(redirect_url)

@app.route('/albums/<int:album_id>/delete_all_photos', methods=['POST'])
@album_access_required
def delete_all_photos_in_album(album_id, album):
    """Deletes all photos within a specific album (DB records and files)."""
    db = get_db(); deleted_db_count = 0; deleted_file_count = 0; error_files = []; photos_to_delete = []
    try:
        photos_to_delete = db.execute('SELECT filename FROM photos WHERE album_id = ?', (album_id,)).fetchall()
        if not photos_to_delete: flash("No photos found in this album to delete.", 'info'); return redirect(url_for('manage_photos_in_album', album_id=album_id))
        cursor = db.execute('DELETE FROM photos WHERE album_id = ?', (album_id,)); deleted_db_count = cursor.rowcount; db.commit()
        print(f"Deleted {deleted_db_count} photo records from DB for album ID: {album_id}.")
        print(f"Attempting to delete {len(photos_to_delete)} associated files...")
        for photo in photos_to_delete:
            filename = photo['filename'];
            if not filename: continue
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            try:
                if os.path.isfile(filepath): os.unlink(filepath); deleted_file_count += 1
            except OSError as e: print(f"  Error deleting file {filepath}: {e}"); error_files.append(filename)
        print(f"Finished deleting {deleted_file_count} files.")
        if deleted_db_count > 0: flash(f"Successfully deleted {deleted_db_count} photos from album '{album['title']}'.", 'success')
        if deleted_file_count < deleted_db_count and not error_files: flash(f"Note: {deleted_db_count - deleted_file_count} file(s) were already missing.", "warning")
        if error_files: flash(f"Failed to delete {len(error_files)} file(s): {', '.join(error_files)}", 'error')
    except Exception as e: import traceback; print(f"ERROR deleting all photos for album {album_id}: {e}"); traceback.print_exc(); flash(f"An error occurred while deleting photos: {e}", 'error')
    return redirect(url_for('manage_photos_in_album', album_id=album_id))

@app.route('/albums/<int:album_id>/delete', methods=['POST'])
@album_access_required
def delete_album(album_id, album):
    if album['title'] == DEFAULT_ALBUM_TITLE: flash(f"The '{DEFAULT_ALBUM_TITLE}' album cannot be deleted.", 'error'); return redirect(url_for('list_albums'))
    cover_filename = album['cover_filename']; cover_filepath = os.path.join(app.config['UPLOAD_FOLDER'], cover_filename); photos_in_album = []; db = get_db()
    try:
        photos_in_album = db.execute('SELECT filename FROM photos WHERE album_id = ?', (album_id,)).fetchall()
        print(f"Deleting album ID: {album_id}, Title: {album['title']}")
        db.execute('DELETE FROM albums WHERE id = ?', (album_id,)); db.commit(); print(f"Deleted album record.")
        deleted_photo_files = 0; error_photo_files = []
        for photo in photos_in_album:
             photo_filename = photo['filename']; photo_filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
             if os.path.exists(photo_filepath):
                 try: os.remove(photo_filepath); deleted_photo_files += 1
                 except OSError as e: print(f"Err del photo file {photo_filename}: {e}"); error_photo_files.append(photo_filename)
        if error_photo_files: flash(f'Could not delete {len(error_photo_files)} photo file(s).', 'error')
        if cover_filename != DEFAULT_COVER_FILENAME and os.path.exists(cover_filepath):
             try: os.remove(cover_filepath); print(f"Deleted cover file: {cover_filepath}")
             except OSError as e: print(f"Err del cover {cover_filepath}: {e}"); flash(f'Album deleted, failed cover delete "{cover_filename}".', 'error')
        flash(f'Album "{album["title"]}" deleted!', 'success')
    except Exception as e: import traceback; print(f"ERR deleting album {album_id}: {e}"); traceback.print_exc(); flash(f'Error deleting album: {e}', 'error'); db.rollback()
    return redirect(url_for('list_albums'))

# --- Main Execution ---
if __name__ == '__main__':
    if not os.path.exists(DATABASE): print(f"WARNING: Database '{DATABASE}' missing. Run 'flask init-db'.")
    if not os.path.exists(UPLOAD_FOLDER): print(f"Upload folder '{UPLOAD_FOLDER}' missing. Creating."); os.makedirs(UPLOAD_FOLDER)
    default_cover_path = os.path.join(app.config['UPLOAD_FOLDER'], DEFAULT_COVER_FILENAME)
    if not os.path.exists(default_cover_path): print(f"WARNING: Default cover '{DEFAULT_COVER_FILENAME}' missing in '{UPLOAD_FOLDER}'.")
    app.run(debug=True)
