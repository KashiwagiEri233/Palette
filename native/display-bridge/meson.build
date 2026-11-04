project('palette-display-bridge', 'c', version: '0.1.0',
  default_options: ['c_std=c11', 'warning_level=2', 'werror=true'])

gio = dependency('gio-2.0', version: '>=2.66')
deps = [gio]
if host_machine.system() != 'windows'
  deps += dependency('gio-unix-2.0')
else
  deps += meson.get_compiler('c').find_library('ws2_32')
endif
executable('palette-display-bridge', 'main.c', dependencies: deps, install: true)
