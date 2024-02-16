#!/usr/bin/python3
# SPDX-License-Identifier: LGPL-2.1-or-later

'''Test wrapper command for driving integration tests.

Note: This is deliberately rough and only intended to drive existing tests
with the expectation that as part of formally defining the API it will be tidy.

'''

from argparse import ArgumentParser
from contextlib import contextmanager, suppress
from fcntl import flock, LOCK_EX, LOCK_NB, LOCK_UN
from hashlib import sha256
from json import loads as json_loads
from importlib.util import module_from_spec, spec_from_file_location
from logging import getLogger as logging_get_logger, basicConfig as logging_basic_config, DEBUG as LOG_DEBUG
from os import geteuid, getpid, rename
from pathlib import Path
from shlex import quote
from shutil import copyfileobj, rmtree
from subprocess import run, STDOUT
from sys import modules as sys_modules, stdout
from tempfile import NamedTemporaryFile, TemporaryDirectory


log = logging_get_logger(__name__)


# TODO: Deduplicate this with top-level mkosi.conf somehow
BUILTIN_KERNEL_COMMAND_LINE_ARGS = (
    'systemd.emergency=exit',
    'systemd.crash_shell',
    'systemd.log_level=debug',
    'systemd.log_ratelimit_kmsg=0',
    'systemd.journald.forward_to_console',
    'systemd.journald.max_level_console=warning',
    'printk.devkmsg=on',
    'rw',
    'systemd.deault_device_timeout_sec=20',
    'systemd.early_core_pattern=/core',
    'systemd.firstboot=no',
)

parser = ArgumentParser(description=__doc__)
parser.add_argument('--mkosi-image-name', required=True)
parser.add_argument('--mkosi-output-path', required=True, type=Path)
parser.add_argument('--test-unit-name', required=True)
parser.add_argument('--hook-module', type=Path, default=None)
parser.add_argument('mkosi_args', nargs="*")


@contextmanager
def lock_file(path):
    '''Lock the provided path, exiting if already locked

    This creates a lockfile alongside called `.{path}.lock`
    and writes the PID of the process that locked it
    so that lock failures report which process holds it open.

    The lock is released when the context or process exits.
    '''
    lockfile = path.with_name(f".{path.name}.lock")

    fobj = open(lockfile, mode='a+')
    try:
        flock(fobj, LOCK_EX|LOCK_NB)
    except BlockingIOError:
        exit(f"{path} already being written to by {fobj.read()}")

    fobj.seek(0)
    fobj.truncate(0)
    fobj.write(str(getpid()))

    log.debug(f"Holding exclusive lock on lockfile {lockfile}")
    try:
        yield
    finally:
        log.debug(f"Releasing exclusive lock on lockfile {lockfile}")
        flock(fobj, LOCK_UN)


@contextmanager
def dissect_mount(image):
    '''Mount the image with systemd-dissect, yielding the mountpoint'''
    with TemporaryDirectory(prefix='.dissect', suffix='.mountpoint') as td:
        log.debug(f"Mounting {image} to {td}")
        run(['systemd-dissect', '--mount', image, td], check=True)
        try:
            yield td
        finally:
            log.debug(f"Unmounting {image}")
            run(['systemd-dissect', '--umount', td], check=True)


def main():
    logging_basic_config(level=LOG_DEBUG)
    args = parser.parse_args()

    log.debug(f"image: {args.mkosi_image_name}\n"
              f"mkosi output path: {args.mkosi_output_path}\n"
              f"test unit name: {args.test_unit_name}\n"
              f"mkosi args: {args.mkosi_args}\n"
              f"hook module: {args.hook_module}")

    per_test_output_dir = args.mkosi_output_path.with_name(args.test_unit_name)
    log.debug(f"Copying {args.mkosi_output_path.absolute()} output dir "
              f"to per-test path {per_test_output_dir.absolute()}")

    hook = None
    if args.hook_module is not None:
        spec = spec_from_file_location('hook', args.hook_module)
        hook = module_from_spec(spec)
        sys_modules['hook'] = hook
        spec.loader.exec_module(hook)

    with lock_file(per_test_output_dir):
        with TemporaryDirectory(dir=per_test_output_dir.parent.absolute(),
                                prefix=f".{per_test_output_dir.name}",
                                suffix=".tmp") as td:
            log.debug(f"Copying to temporary path {td}")
            if geteuid() == 0:
                cp_args = [
                    'cp',
                    '--archive',
                    '--reflink=auto',
                    '--no-target-directory',
                    args.mkosi_output_path,
                    td,
                ]
                if log.isEnabledFor(LOG_DEBUG):
                    log.debug(
                        "Using rootly copy of output directory with command "
                        + ' '.join(quote(str(a)) for a in cp_args))
                run(cp_args, check=True)
            else:
                log.debug("Using namespace aware copy of build directory")
                for e in listdir(args.mkosi_output_path):
                    src = args.mkosi_output_path / e
                    dst = Path(td) / e
                    cp_args = []

                    if src.is_dir():
                        cp_args += ['unshare', '--map-auto', '--map-root-user']

                    args += ['cp', '--archive', '--reflink=auto', src, dst]
                    run(cp_args, check=True)

            log.debug(f"Cleaning old dir {per_test_output_dir.absolute()}")
            with suppress(FileNotFoundError):
                rmtree(per_test_output_dir)

            log.debug(f"Renaming tempdir {td} onto "
                      f"{per_test_output_dir.absolute()}")
            rename(td, per_test_output_dir)

        def run_mkosi(mkosi_args, **kwargs):
            clargs = [
                'mkosi',
                '--directory=..',
                '--image', args.mkosi_image_name,
                '--format=disk',
                '--output-dir', per_test_output_dir.absolute(),
            ] + mkosi_args
            if log.isEnabledFor(LOG_DEBUG):
                log.debug(f"Running {' '.join(quote(str(a)) for a in clargs)}")

            return run(clargs, **kwargs)

        machine_id = sha256(args.test_unit_name.encode()).hexdigest()[:32]
        log.debug(f"Derived machine-id {machine_id} for VM")

        with NamedTemporaryFile() as console_log:
            log.debug(f"Capturing mkosi console log to {console_log.name}")

            run_mkosi(['--qemu-smp=2',
                       '--qemu-mem=2G',
                       '--credential', f"system.machine_id={machine_id}",
                       '--kernel-command-line-extra',
                       ' '.join(BUILTIN_KERNEL_COMMAND_LINE_ARGS),
                      ] + args.mkosi_args + ['qemu'],
                      check=True, stderr=STDOUT, stdout=console_log)
            console_log.seek(0)
            copyfileobj(console_log, stdout.buffer)

            result = run_mkosi(['--json', 'summary'],
                               capture_output=True,
                               check=True)
            summary = json_loads(result.stdout)
            image_path = [f"{image['OutputDirectory']}/{image['Output']}"
                          for image in summary['Images']
                          if image['Image'] == args.mkosi_image_name][0]
            log.debug(f"Discovered disk image path {image_path}")

            mounted_disk = dissect_mount
            if hook is not None and hasattr(hook, 'mounted_disk'):
                log.debug("Using hook.mounted_disk")
                mounted_disk = hook.mounted_disk

            try:
                with mounted_disk(image_path) as mountpoint:
                    mountpoint = Path(mountpoint)

                    if hook is not None and hasattr(hook, 'check_result'):
                        log.debug("Running check_result hook")
                        console_log.seek(0)
                        hook.check_result(mountpoint, console_log)

                    if not (mountpoint / "testok").exists():
                        log.debug("Test failed, /testok not present")
                        exit(1)
            except BaseException:
                log.debug("Attempting journalctl to discover test failure")
                run_mkosi(['journalctl',
                           '--boot',
                           '--unit', args.test_unit_name,
                          ])
                raise
            else:
                log.debug("Test succeeded, cleaning up")
                rmtree(per_test_output_dir)


if __name__ == '__main__':
    main()
