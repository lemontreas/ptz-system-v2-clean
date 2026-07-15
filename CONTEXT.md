# Context Glossary

## Terms

### Field installer

A person who can safely use common installation tools but has no prior knowledge of this device's component relationships, alignment requirements, cabling constraints, or installation acceptance criteria.

_Avoid_: Product developer, trained device specialist, ordinary end user

### Antenna assembly datum orientation

The prescribed rotational orientation of the antenna connector interface used as the datum for installing the camera, PTZ, cabling, and subsequent mounting parts. A different orientation may still allow assembly, but creates avoidable installation difficulty and rework risk; installers therefore verify the visible hardware feature or mark before proceeding.

_Avoid_: Cosmetic orientation, installer preference, approximate direction

### Local assembly reference

A close-up relationship between two visible features on mating parts that defines the correct installation position without relying on global left/right directions or the installer's viewing angle.

_Avoid_: Camera-relative left/right, approximate placement, whole-device viewing direction

### Antenna–camera assembly

The field-assembled upper unit formed by mounting the camera above the antenna in the prescribed relative position. The camera and antenna are delivered as separate parts but are treated as one unit when subsequently connected to the PTZ.

_Avoid_: Factory-integrated antenna, preassembled camera, camera-only unit

### PTZ–bracket assembly

The field-assembled support unit formed by connecting the complete, factory-built PTZ to its lower bracket and associated cable-retention component. The PTZ itself is not assembled or serviced in the field.

_Avoid_: PTZ internal assembly, separate PTZ installation module, field-built PTZ

### Mechanical-before-cabling sequence

The required field-installation order in which the antenna–camera assembly and its supporting hardware are mechanically installed and secured before any associated cables are connected.

_Avoid_: Connecting cables while supporting a loose assembly, cabling-first installation

### Two-stage cable retention

The field-installation practice of first placing the cable in the bracket's prescribed routing slot without final restraint, then connecting it and applying final restraint only after verifying adequate PTZ movement slack and freedom from interference.

_Avoid_: Final cable tightening during bracket installation, leaving the cable unrestrained after acceptance

### Installation module card

A document section that identifies one hardware module with a correct-state image, names its included parts, explains each relevant component's purpose, highlights module-specific constraints, and links to the corresponding installation video.

_Avoid_: Complete assembly sequence, chronological installation procedure

### System assembly sequence

The numbered end-to-end procedure that defines how completed hardware modules are positioned, supported, moved, connected, checked, and secured into the final device.

_Avoid_: Module description, parts inventory, unordered collection of installation tips

### Full-scan activity flag

`full_scan.active` indicates whether the front end should continue presenting a full-area scan as actively running.

When a stop request is accepted, it becomes `false` immediately even though worker cleanup may still be in progress. During that cleanup interval, `state="stopping"`, `stop_requested=true`, and `terminal=false` remain the authoritative lifecycle indicators. A terminal stop is reached only after cleanup completes.

_Avoid_: Worker-cleanup-complete flag, permission to start another scan

### Full-scan stop response budget

A manual full-scan stop has a hard two-second response budget for every stage: API-visible state transition, stop propagation, capture/session cleanup, and worker terminal-state publication.

The budget is end-to-end rather than a separate two-second allowance for each blocking cleanup operation.

_Avoid_: Best-effort stop, ten-second cleanup grace period, per-operation two-second accumulation

### Manual alignment angle

The PTZ pan/tilt reading recorded after an operator manually moves the camera until the selected visual target is close enough to the desired alignment point that the remaining visual error is considered negligible.

This is a calibration reference value, not a value computed by the panorama or single-image pixel conversion model.

### Camera-to-PTZ center offset

The physical displacement from the PTZ rotation center to the camera optical center.

For the current device, viewed from behind the device, the camera optical center is approximately 9 cm to the right of the PTZ center axis, approximately 40 cm vertically offset from the PTZ center axis, and approximately 18-19 cm forward of the PTZ rotation center. This offset is a parallax input for distance-aware visual angle correction, not the same concept as RF antenna bias.

### Full-scan point marker

A synthetic record that marks a PTZ point as visited during a full-area scan even when no real WiFi device was detected there. It exists for scan-path visualization and diagnostics, not for device discovery or whitelist eligibility.

_Avoid_: Broadcast device, fake AP, whitelist candidate

### Full-scan fixed-point evidence

WiFi evidence collected during the configured sampling window after the PTZ has reached a formal scan point and completed its stabilization wait.

_Avoid_: Movement evidence, settling evidence, path evidence

### Full-scan directional RSSI representative

The strongest single-packet directional RSSI observed for a MAC during one eligible sampling window. The historical `rssi_avg` field carries this peak value and must not be interpreted as an arithmetic mean.

_Avoid_: Mean directional RSSI, averaged RSSI

### Hidden SSID sentinel

The reserved AP `ssid` value `_wildcard_` means a captured Beacon explicitly carried a zero-length or all-zero SSID information element. A `null` SSID instead means the AP name was unavailable or could not be determined.

_Avoid_: Empty-string hidden SSID, unknown SSID

### Full-scan point-priority traversal

A scan traversal strategy that visits each formal scan point once and cycles through all required WiFi configurations before moving to the next point.

_Avoid_: Legacy scan, old strategy

### Full-scan configuration-priority continuous collection

A scan traversal strategy that keeps one WiFi configuration active while traversing all formal scan points, then repeats the point path for the next configuration.

_Avoid_: New strategy, configuration-priority scan without continuous collection

### Full-scan path evidence

WiFi evidence collected while the PTZ is moving toward a formal scan point or waiting for its motion to stabilize. It is auxiliary evidence and does not count as a formal fixed-point hit.

_Avoid_: Fixed-point evidence, formal point hit

### Full-scan path midpoint

The panorama-pixel midpoint between two consecutive formal scan points, used as the approximate spatial representative for their aggregated Full-scan path evidence.

It is an evidence location estimate, not a claim that the PTZ passed through that exact panorama pixel.

_Avoid_: Formal scan point, exact measured packet position

### Full-scan movement relay

A temporary non-sampling PTZ pose used to overcome a movement that is too small or unreliable before approaching the intended formal scan point again.

Evidence collected during the relay detour is discarded; the relay is not a formal scan point or path-evidence point.

_Avoid_: Formal scan point, path midpoint, additional sampling point

### Full-scan whitelist refinement

Post-decision evidence collection for improving a whitelisted device's displayed approximate strongest position without changing whether the device belongs to the whitelist. When enabled, it is a non-terminal tail phase of the same Full-scan task, so Full-scan remains active until refinement completes, fails, or is stopped.

_Avoid_: Whitelist eligibility check, additional whitelist filter

### Full-scan peak-packet trajectory position

The displayed approximate position of a refinement peak packet, estimated by interpolating between the nearest observed PTZ poses and then selecting the closest valid pixel constrained to its original refinement segment.

_Avoid_: Whole-segment midpoint, unconstrained angle-to-pixel reverse lookup, uniform-speed whole-segment estimate, exact RF source position

### Whitelist decision configuration

The single actually observed channel and scan bandwidth whose evidence is eligible for deciding a MAC's whitelist membership. A client prefers its relationship-derived AP configuration when observed there, otherwise it falls back to its strongest actually observed configuration; evidence from all other configurations is excluded but retained for diagnostics. Final whitelist output exposes every actually observed configuration as a compact `observed_configs` summary and marks exactly one as `selected=true`; a MAC rejected because no valid decision configuration exists may have no selected summary.

_Avoid_: Unobserved relationship configuration, cross-configuration evidence mixture, any-passing configuration

### Client–AP communication relationship

Passive evidence that a WiFi client and an access point exchanged an IEEE 802.11 Data frame. Management frames, probe traffic, and channel coincidence may provide discovery context but do not establish this relationship.

_Avoid_: Probe target, attempted association, same-channel device

### Current observed AP

The AP in the most recently observed Data-frame relationship for a client. It describes the latest passive observation and does not guarantee that the client remains associated at query time.

_Avoid_: Guaranteed current connection, configured AP

### Scan bandwidth

The receiver channel width used to collect WiFi evidence during a scan.

_Avoid_: AP declared operating bandwidth

### AP declared operating bandwidth

The operating channel width advertised by an AP through Beacon HT/VHT operation information. It is device metadata and does not describe the receiver width used by the scan.

_Avoid_: Scan bandwidth

### AP authoritative channel evidence

Full-scan evidence for a known AP that was collected while the receiver was tuned to the AP's Beacon-declared primary channel. Off-channel observations of that AP are not formal full-scan evidence.

_Avoid_: Adjacent-channel observation, strongest off-channel observation

### AP inferred channel

The receiver channel on which an AP's Beacon was observed when the Beacon contains no usable primary-channel declaration. It may be used as fallback scan evidence but must remain distinguishable from a Beacon-declared channel.

_Avoid_: AP authoritative channel, confirmed primary channel

### Full-scan image context

The single image or panorama whose pixel coordinate system defines all ranges, formal points, path midpoints, and displayed positions for one full-scan task. The context remains fixed for the lifetime of that task.

_Avoid_: Latest image after task start, mixed-image pixel coordinates

### Full-scan outer range

The broad pixel range on the locked Full-scan image context used to compare the target area against its surroundings. At task start it is read from Redis `gimbal:default_config.work_x_range/work_y_range`; it is not supplied as an angle range in the request payload.

_Avoid_: Target range, `precheck_range`, angle work range

### Full-scan target range

The smaller pixel area whose likely resident devices the full-area scan is trying to identify. In the start API payload this is represented by the single-item `target_ranges` array containing `x_range` and `y_range`.

_Avoid_: Outer range, angle `work_ranges`

### Full-scan coarse range

The whole pixel range covered by the full-area scan coarse pass to understand the surrounding RF environment. It currently coincides with the Full-scan outer range read from `work_x_range/work_y_range`.

_Avoid_: Target range, angle work range

### Full-scan outer probe points

The coarse-pass boundary probe points generated outside the Full-scan coarse range to catch stronger RF evidence from nearby surrounding areas.

_Avoid_: Target-range boundary points, deviation-ring points

### Whitelist decision buffer

An expanded evidence area around the Full-scan target range used only when classifying full-scan whitelist evidence near the target boundary.

_Avoid_: Enlarged target range, scan range, front-end display range

### PMAP authoritative mapping

The binary `pixel_map.pmap` generated by the rebuilt Hugin/nona pipeline is the authoritative mapping for Hugin panorama coordinate conversion.

For Hugin panoramas, both pixel-to-angle and angle-to-pixel conversion should use PMAP as the primary source of truth. Legacy `coordinate_map.npz` and `correction_map.npz` may exist for fallback or diagnostics, but they must not define the default bidirectional conversion loop when PMAP is available.

_Avoid_: Treating `coordinate_map.npz` as equal authority for Hugin panorama round-trip conversion

### PMAP reverse lookup

The operation of locating a panorama pixel for a PTZ angle by using `pixel_map.pmap` as the source of truth.

The lookup projects the PTZ angle into source-image pixel space, then finds the panorama pixel whose PMAP owner/source coordinate is closest. This keeps angle-to-pixel conversion in the same PMAP coordinate model as pixel-to-angle conversion.

_Avoid_: Linear panorama mapping, `coordinate_map.npz` reverse lookup when PMAP is available
