# Source modification notes for nona

This is a version-tolerant guide rather than a blind patch, because Hugin source
layout differs between distro and upstream versions.

## Add command-line state

In the nona executable source, add:

```cpp
#include <fstream>
#include <iomanip>
```

Add state near the other command-line options:

```cpp
std::string pixelMapPath;
```

Register a long option:

```cpp
--pixel-map FILE
```

Set `pixelMapPath` from the option parser.

## Open the CSV

After the PTO is loaded and before remapping starts:

```cpp
std::ofstream pixelMap;
if (!pixelMapPath.empty()) {
    pixelMap.open(pixelMapPath.c_str(), std::ios::out | std::ios::trunc);
    if (!pixelMap) {
        std::cerr << "nona: unable to open pixel map: " << pixelMapPath << std::endl;
        return 1;
    }
    pixelMap << "pano_x,pano_y,image_index,source_x,source_y\n";
    pixelMap << std::fixed << std::setprecision(6);
}
```

If this point has access to crop metadata, write it before the header:

```cpp
pixelMap << "# full_width=" << fullWidth << "\n";
pixelMap << "# full_height=" << fullHeight << "\n";
pixelMap << "# crop_left=" << cropLeft << "\n";
pixelMap << "# crop_top=" << cropTop << "\n";
pixelMap << "# crop_right=" << cropRight << "\n";
pixelMap << "# crop_bottom=" << cropBottom << "\n";
pixelMap << "pano_x,pano_y,image_index,source_x,source_y\n";
```

## Where to write rows

The best insertion point is inside the existing per-source-image remapping
worker, right after nona has computed the source coordinate for a destination
panorama pixel and decided it is valid.

Preferred shape:

```cpp
if (pixelMap) {
    pixelMap << dstX << ','
             << dstY << ','
             << imageIndex << ','
             << srcX << ','
             << srcY << '\n';
}
```

This is preferred because it uses the same transform and validity test as the
actual remapper.

## Fallback if the remap loop is hard to access

If the existing remap worker hides `(dstX, dstY) -> (srcX, srcY)`, add a second
explicit geometry pass after loading the PTO. This is slower but much easier to
debug.

The Hugin/Panotools API names vary slightly by version, but the fallback pass is
conceptually:

```cpp
PanoramaOptions opts = pano.getOptions();
vigra::Size2D size = opts.getSize();

for (unsigned imageIndex = 0; imageIndex < pano.getNrOfImages(); ++imageIndex) {
    const SrcPanoImage& src = pano.getImage(imageIndex);
    PTools::Transform transform;
    transform.createInvTransform(src, opts);

    for (int y = 0; y < size.height(); ++y) {
        for (int x = 0; x < size.width(); ++x) {
            double sx = 0.0;
            double sy = 0.0;
            bool ok = transform.transformImgCoord(sx, sy, x + 0.5, y + 0.5);
            if (!ok) {
                continue;
            }
            if (sx < 0.0 || sy < 0.0 ||
                sx >= static_cast<double>(src.getSize().width()) ||
                sy >= static_cast<double>(src.getSize().height())) {
                continue;
            }
            pixelMap << x << ',' << y << ',' << imageIndex << ','
                     << sx << ',' << sy << '\n';
        }
    }
}
```

Notes:

```text
Use x+0.5/y+0.5 if nona samples pixel centers.
Use x/y if the local remapper uses integer destination coordinates.
Match whichever convention gives the closest color/feature check.
```

## Candidate ownership

Do not force uniqueness inside nona at first. Write all candidates:

```text
same pano_x,pano_y may appear multiple times with different image_index values
```

The downstream checker can choose an owner later. For example:

```text
choose the candidate closest to the source image center
or choose the first/last candidate
or keep all candidates and expose coverage_count
```

This keeps the geometry truth intact.

## Performance warning

CSV is intentionally simple for first validation. It can be large:

```text
width * height * overlap_count rows
```

After validation, replace CSV with a binary format or let Python convert it:

```bash
python tools/nona_pixelmap/nona_pixelmap_verify.py to-npz pixel_map.csv pixel_map.npz
```
