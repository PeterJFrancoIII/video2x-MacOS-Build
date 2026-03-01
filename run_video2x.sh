#!/bin/bash
export VK_ENABLE_PORTABILITY_ENUMERATION=1
export VK_ICD_FILENAMES="/usr/local/etc/vulkan/icd.d/MoltenVK_icd.json"
export DYLD_LIBRARY_PATH="/Users/computer/Video2X/build/video2x-install/lib:/usr/local/lib:$DYLD_LIBRARY_PATH"
/Users/computer/Video2X/build/video2x "$@"
