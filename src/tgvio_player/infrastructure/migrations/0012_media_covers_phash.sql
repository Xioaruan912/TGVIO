PRAGMA foreign_keys=ON;

-- A 64-bit perceptual fingerprint of the cover frame, as 16 lowercase hex digits.
-- Optional: a cover without one (an older index, or a frame the worker could not
-- hash) simply means "no similarity information" and is never an error. Additive on
-- purpose - the covers.json schema and algorithm strings must not change, because the
-- Player compares them exactly and would reject a package whose index it cannot read.
ALTER TABLE media_covers ADD COLUMN phash TEXT;
