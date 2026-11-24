# Writable applications on this NOMMU image live on the persistent home volume.
if [ -d "$HOME/.local/bin" ]; then
	PATH="$HOME/.local/bin:$PATH"
	export PATH
fi
