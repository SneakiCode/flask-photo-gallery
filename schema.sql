-- static/schema.sql (Updated)

DROP TABLE IF EXISTS photos;
DROP TABLE IF EXISTS albums;

CREATE TABLE albums (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL UNIQUE,
  description TEXT,
  cover_filename TEXT NOT NULL,
  password_hash TEXT, -- HASHED password, NULL if no password
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  album_id INTEGER NOT NULL,
  filename TEXT NOT NULL UNIQUE,
  caption TEXT, -- <<< CHANGED: Removed NOT NULL constraint
  uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (album_id) REFERENCES albums (id) ON DELETE CASCADE
);

-- Ensure default album has NO password and specific title for deletion check
-- Make sure 'default_cover.png' exists in uploads/
INSERT INTO albums (title, description, cover_filename) VALUES
  ('General Photos', 'A place for miscellaneous photos accessible to everyone.', 'default_cover.png');