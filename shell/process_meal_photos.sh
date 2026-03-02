#!/bin/bash
# Process meal photos: resize for optimal vision model input
# Usage: ./process_meal_photos.sh photo1.jpg [photo2.jpg ...]
# Output: Creates resized versions in /tmp/nutrition-photos/

set -e

OUTPUT_DIR="/tmp/nutrition-photos"
MAX_DIMENSION=1920  # Max pixels on longest side
QUALITY=85          # JPEG quality

mkdir -p "$OUTPUT_DIR"

if [ $# -eq 0 ]; then
    echo "Usage: $0 photo1.jpg [photo2.jpg ...]"
    exit 1
fi

echo "📸 Processing $# photo(s) for nutrition logging..."
echo ""

TOTAL_ORIG=0
TOTAL_NEW=0

for INPUT in "$@"; do
    if [ ! -f "$INPUT" ]; then
        echo "⚠️  Skipping (not found): $INPUT"
        continue
    fi
    
    BASENAME=$(basename "$INPUT")
    OUTPUT="$OUTPUT_DIR/${BASENAME%.*}_resized.jpg"
    
    # Get original dimensions
    DIMS=$(sips -g pixelWidth -g pixelHeight "$INPUT" 2>/dev/null)
    WIDTH=$(echo "$DIMS" | grep pixelWidth | awk '{print $2}')
    HEIGHT=$(echo "$DIMS" | grep pixelHeight | awk '{print $2}')
    ORIG_SIZE=$(stat -f%z "$INPUT")
    TOTAL_ORIG=$((TOTAL_ORIG + ORIG_SIZE))
    
    # Calculate new dimensions
    if [ "$WIDTH" -gt "$HEIGHT" ]; then
        if [ "$WIDTH" -gt "$MAX_DIMENSION" ]; then
            NEW_WIDTH=$MAX_DIMENSION
            NEW_HEIGHT=$((HEIGHT * MAX_DIMENSION / WIDTH))
        else
            NEW_WIDTH=$WIDTH
            NEW_HEIGHT=$HEIGHT
        fi
    else
        if [ "$HEIGHT" -gt "$MAX_DIMENSION" ]; then
            NEW_HEIGHT=$MAX_DIMENSION
            NEW_WIDTH=$((WIDTH * MAX_DIMENSION / HEIGHT))
        else
            NEW_WIDTH=$WIDTH
            NEW_HEIGHT=$HEIGHT
        fi
    fi
    
    # Resize with sips
    cp "$INPUT" "$OUTPUT"
    sips --resampleHeightWidth "$NEW_HEIGHT" "$NEW_WIDTH" -s format jpeg -s formatOptions $QUALITY "$OUTPUT" >/dev/null 2>&1
    
    NEW_SIZE=$(stat -f%z "$OUTPUT")
    TOTAL_NEW=$((TOTAL_NEW + NEW_SIZE))
    
    ORIG_KB=$(echo "scale=0; $ORIG_SIZE/1024" | bc)
    NEW_KB=$(echo "scale=0; $NEW_SIZE/1024" | bc)
    SAVINGS=$(echo "scale=0; 100-($NEW_SIZE*100/$ORIG_SIZE)" | bc)
    
    echo "✓ $BASENAME"
    echo "  ${WIDTH}x${HEIGHT} → ${NEW_WIDTH}x${NEW_HEIGHT}"
    echo "  ${ORIG_KB}KB → ${NEW_KB}KB (-${SAVINGS}%)"
    echo ""
done

TOTAL_ORIG_KB=$(echo "scale=0; $TOTAL_ORIG/1024" | bc)
TOTAL_NEW_KB=$(echo "scale=0; $TOTAL_NEW/1024" | bc)
TOTAL_SAVINGS=$(echo "scale=0; 100-($TOTAL_NEW*100/$TOTAL_ORIG)" | bc)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Total: ${TOTAL_ORIG_KB}KB → ${TOTAL_NEW_KB}KB (-${TOTAL_SAVINGS}%)"
echo "📁 Output: $OUTPUT_DIR/"
echo ""
ls -la "$OUTPUT_DIR"/*.jpg 2>/dev/null || true
