# Sources and third-party materials

The Python implementation in this repository is distributed under the
[MIT License](LICENSE). Firmware has separate terms and is not included in
the source tree, wheel, or source distribution.

## Protocol and file-format references

- [JoshuaDoes/tensor-usbdl](https://github.com/JoshuaDoes/tensor-usbdl)
  documents the observed Tensor USB download dialogue. Its upstream source
  is licensed under AGPL-3.0. The pinned recovery release is
  [v0.2.0, tag `020`](https://github.com/JoshuaDoes/tensor-usbdl/releases/tag/020).
- [hack-different/pixel-restore](https://github.com/hack-different/pixel-restore)
  provided additional protocol, stage-mapping, and recovery-pack observations.
- Google's published
  [fbpack.py](https://developers.google.com/android/binary_transparency/tools/pixel/fbpacktool/fbpack.py)
  defines the FBPK v2 layout and is licensed under Apache-2.0.

Those upstream source files and executables are not bundled or executed by
pixel-eub. See [protocol notes](docs/protocol.md) for the implemented wire format.

## Downloads

At the user's request, pixel-eub downloads the pinned tensor-usbdl release and
reads only its selected `sources/` images. It does not execute the release's
programs or scripts. Downloading an archive does not change the rights to
redistribute its contents.

Factory images come from Google's
[factory image catalog](https://developers.google.com/android/images) and are
subject to Google's terms. Do not attach firmware, device backups, or dumps to
issues, pull requests, or releases of this project.

## Runtime dependency

[pySerial](https://github.com/pyserial/pyserial) is installed separately under
its upstream BSD license; it is not vendored in this repository.

pixel-eub is a community project and is not affiliated with Google or Samsung.
