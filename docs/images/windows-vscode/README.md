# Windows walkthrough screenshots

Captured on 2026-10-02 from the Windows 11 lab desktop through RDP in Xephyr.
Images are real application screenshots, cropped for readability. They are
shared by the SITL and Renode walkthroughs; no generated artwork is used.

The demonstration uses a separate AM32 checkout at
`2738df3240baa5bd4295b460cf0c5cfe0bd49d97` and VS Code profile, Microsoft C/C++
1.34.4, the installed ESCSim controls with the extended-stream and Windows
source-path fixes, and the bundled Renode runtime. Local folder names in the
images are examples, not installer requirements. See the
[validation record](../../windows-testing.md#fresh-checkout-walkthrough-validation)
for the SITL Defender limitation and timing fix.

`01`–`09` cover common setup (there is no `08` image); `sitl-*` and `renode-*`
cover the corresponding launch, breakpoint, controls and scope stages.
The SITL scope uses fine capture at 0.5 µs/sample. The Renode scope uses
50 µs/sample and 500 µs/div. Both captures were saved as CSV plus setup JSON
in the lab's `Desktop/AM32/ESCSim/docs-demo` folder.

When updating an image, reproduce its step with the released installer and
current firmware, capture the actual window, retain visible labels needed
by the instructions, and update the validation record if behaviour changes.
