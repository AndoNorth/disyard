# Sourced by the flake shellHook. Pins uv to the flake's Python.
# UV_PYTHON_PREFERENCE is required: UV_PYTHON_DOWNLOADS=never alone still
# lets uv pick a managed Python already in ~/.local/share/uv/python.
export UV_PYTHON_DOWNLOADS=never
export UV_PYTHON_PREFERENCE=only-system
# UV_PYTHON breaks `uv pip`, so it must stay unset.
unset UV_PYTHON

# manylinux wheels (numpy) need libstdc++, which the Nix interpreter does not
# find on its own. Expose only libstdc++, libgcc_s and libz from the host through a
# private directory: putting a whole host lib dir on the path would also
# shadow Nix's libc. WFT_LD_EXTRA=<dir>[:<dir>...] appends further directories.
_wft_ld="${XDG_CACHE_HOME:-$HOME/.cache}/weft/ld"
for _d in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/lib /lib/x86_64-linux-gnu; do
  if [ -e "$_d/libstdc++.so.6" ]; then
    mkdir -p "$_wft_ld"
    for _l in libstdc++.so.6 libgcc_s.so.1 libz.so.1; do
      [ -e "$_d/$_l" ] && ln -sf "$_d/$_l" "$_wft_ld/$_l"
    done
    export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$_wft_ld"
    break
  fi
done
unset _d _l _wft_ld
if [ -n "${WFT_LD_EXTRA:-}" ]; then
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$WFT_LD_EXTRA"
fi
