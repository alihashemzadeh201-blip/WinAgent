# 3D software (Blender, 3ds Max, SketchUp, CAD)

### When to use
Any task inside a 3D program: moving / rotating / renaming / deleting objects, changing the camera,
exporting. The core problem is OCCLUSION: an object hidden behind another cannot be clicked reliably.

### Steps
1. `open_app` the program, then `screenshot`. Identify the VIEWPORT (large 3D area) vs panels
   (Blender: Outliner left, properties right).
2. NEVER click an object you cannot fully see. A viewport click always hits the FRONTMOST object,
   so a guessed click selects the wrong thing and ruins the task.
3. Change the viewpoint FIRST: orbit with `drag` button="middle" (start near the viewport centre),
   zoom with `scroll`. Blender numpad: `press_keys` num1=front, num3=right, num7=top, num5=perspective.
   Take a `screenshot` after EVERY viewpoint change and act only on what you actually see.
4. If the target is still hidden, select it WITHOUT the viewport:
   - Blender Outliner (left panel): click the object's NAME — safe, it is a list, not the viewport.
   - Blender F3: press f3, `type_text` the object name (or "Select"), then Enter.
   - Blender menu: `menu` tool with path ["Select", "Select by Name"] (keyboard only).
5. VERIFY the selection on a screenshot: the selected object shows a cyan/white outline. Only then
   operate on the SELECTION: G=move, R=rotate, S=scale (Left click confirms, Esc cancels),
   F2=rename, X or Delete=delete (confirm the "Delete Objects" dialog with Enter), Tab toggles
   edit mode (press Tab again to return to object mode).
6. Overlapping objects: zoom in (`scroll`), orbit to a clear angle, or filter the Outliner
   (Blender alt+f) instead of clicking through the stack.
7. Menus (File / Edit / Export…): always the `menu` tool (keyboard) — never the mouse. Save: ctrl+s.
8. Before `task_complete`, take a final `screenshot` and confirm the requested end state
   (object moved/renamed/deleted, export file visible) — this is the verification pass.

### Notes
- 3ds Max / SketchUp: same occlusion rule; orbit = middle-drag, zoom = wheel; in 3ds Max the
  View Cube (top-right of the viewport) can be clicked to snap to a standard view.
- A stray Left click in the viewport CHANGES the selection (frontmost object). After ANY viewport
  click, re-verify the selection outline before acting.
- If unsure about a confirmation dialog, press Esc instead of Enter, then re-check the screenshot.
- Blender object-level actions (move/rename/delete) need OBJECT mode — if the mode panel says
  "Edit Mode", press Tab first.
- Numpad key names for `press_keys`: num1 … num9, num0 (e.g. {"keys": "num1"}).
- If the viewport is black/blank after a change, take another screenshot; if it stays empty, report
  it honestly in task_complete (success=false) instead of guessing object positions.
