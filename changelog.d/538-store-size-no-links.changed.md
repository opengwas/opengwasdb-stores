- **`ogstores.store_size.store_size_on_disk` counts regular files only and
  follows no symbolic link (#538).** It used to follow a file link, so an
  overlay was counted with its targets' bytes. Symlink-free Stores measure
  exactly as before.
