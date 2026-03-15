#!/bin/bash
# Resize image to max 1920px on longest side (preserves aspect ratio)
# Usage: ./resize_image.sh input.jpg [output.jpg]
# If no output specified, creates input_resized.jpg

INPUT="$1"
OUTPUT="${2:-${INPUT%.*}_resized.jpg}"

if [ -z "$INPUT" ]; then
    echo "Usage: $0 input.jpg [output.jpg]"
    exit 1
fi

if [ ! -f "$INPUT" ]; then
    echo "Error: File not found: $INPUT"
    exit 1
fi

# Get current dimensions
DIMS=$(sips -g pixelWidth -g pixelHeight "$INPUT" 2>/dev/null)
WIDTH=$(echo "$DIMS" | grep pixelWidth | awk '{print $2}')
HEIGHT=$(echo "$DIMS" | grep pixelHeight | awk '{print $2}')

echo "Original: ${WIDTH}x${HEIGHT}"

# Calculate new dimensions (max 1920 on longest side)
MAX=1920
if [ "$WIDTH" -gt "$HEIGHT" ]; then
    if [ "$WIDTH" -gt "$MAX" ]; then
        NEW_WIDTH=$MAX
        NEW_HEIGHT=$((HEIGHT * MAX / WIDTH))
    else
        NEW_WIDTH=$WIDTH
        NEW_HEIGHT=$HEIGHT
    fi
else
    if [ "$HEIGHT" -gt "$MAX" ]; then
        NEW_HEIGHT=$MAX
        NEW_WIDTH=$((WIDTH * MAX / HEIGHT))
    else
        NEW_WIDTH=$WIDTH
        NEW_HEIGHT=$HEIGHT
    fi
fi

# Copy and resize
cp "$INPUT" "$OUTPUT"
sips --resampleHeightWidth "$NEW_HEIGHT" "$NEW_WIDTH" "$OUTPUT" >/dev/null 2>&1

# Get new file size
ORIG_SIZE=$(stat -f%z "$INPUT")
NEW_SIZE=$(stat -f%z "$OUTPUT")

echo "Resized: ${NEW_WIDTH}x${NEW_HEIGHT}"
echo "Size: $(echo "scale=1; $ORIG_SIZE/1024" | bc)KB → $(echo "scale=1; $NEW_SIZE/1024" | bc)KB"
echo "Output: $OUTPUT"
