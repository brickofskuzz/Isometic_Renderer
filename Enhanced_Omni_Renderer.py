import bpy
import sys
import subprocess
import os
import math
import tempfile
import time
import statistics
from bpy.props import (
    BoolProperty, IntProperty, EnumProperty, PointerProperty,
    StringProperty, CollectionProperty, FloatProperty
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Vector

def ensure_pillow_installed():
    try:
        import PIL
    except ImportError:
        def draw(self, context):
            self.layout.label(text="Installing Pillow...")
        bpy.context.window_manager.popup_menu(draw, title="Installing", icon='INFO')

        python_exe = sys.executable
        try:
            subprocess.check_call([python_exe, "-m", "ensurepip"])
            subprocess.check_call([python_exe, "-m", "pip", "install", "--upgrade", "pip"])
            subprocess.check_call([python_exe, "-m", "pip", "install", "pillow"])
        except Exception as e:
            def draw_error(self, context):
                self.layout.label(text="Failed to install Pillow.")
                self.layout.label(text=str(e))
            bpy.context.window_manager.popup_menu(draw_error, title="Installation Error", icon='ERROR')
            return

        def draw_done(self, context):
            self.layout.label(text="Pillow installed. Please restart Blender.")
        bpy.context.window_manager.popup_menu(draw_done, title="Installation Complete", icon='CHECKMARK')

# Call on startup
ensure_pillow_installed()


from PIL import Image

bl_info = {
    "name": "Omnidirectional Isometric Render",
    "author": "s3rdia",
    "version": (1, 3),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > OmniIsoRender",
    "description": "Render isometric sprite sheets or single frames from multiple directions",
    "category": "Render",
    "maintainer": "s3rdia"
}


# ---------------------------------------------------------------------------
# Property groups
# ---------------------------------------------------------------------------

class OMNI_ActionItem(PropertyGroup):
    """One row in the multi-select action list."""
    name: StringProperty(name="Action Name")
    selected: BoolProperty(name="Selected", default=False)


class OmniRenderSettings(PropertyGroup):
    resolution_x: IntProperty(name="X", default=400, min=1)
    resolution_y: IntProperty(name="Y", default=400, min=1)
    output_x: IntProperty(name="Output X", default=400, min=1)
    output_y: IntProperty(name="Output Y", default=400, min=1)

    frame_start: IntProperty(name="Start", default=1, min=0)
    frame_end: IntProperty(name="End", default=16, min=0)
    auto_frame_range: BoolProperty(
        name="Auto (match each Action's length)",
        description="Ignore the Start/End fields and automatically use each "
                    "Action's own frame range when exporting",
        default=False
    )

    num_directions: IntProperty(name="Directions", default=8, min=1)
    selected_action: StringProperty(name="Choose Action")

    export_format: EnumProperty(
        name="Format",
        items=[
            ("Single", "Single Images", "Export frames as individual images"),
            ("Spritesheet", "Spritesheet (All Directions)", "Export all frames/directions as a single spritesheet"),
            ("Spritesheet_DIR", "Spritesheet (per Direction)", "Export one spritesheet per direction"),
            ("GIF_DIR", "GIF (per Direction)", "One GIF per direction"),
            ("GIF_ONE", "GIF (one File)", "One GIF for all directions")
        ],
        default="Single"
    )
    measure_time: BoolProperty(name="Measure Render Time", default=False)
    transparent_bg: BoolProperty(name="Transparent Background", default=True)
    export_normals: bpy.props.BoolProperty(name="Export Normal Maps", default=False)

    # Progress display
    show_progress: BoolProperty(
        name="Show Live Progress",
        description="Continuously refresh the panel and cursor progress bar while "
                    "exporting. Adds a small amount of overhead per frame",
        default=True
    )
    progress_text: StringProperty(name="Progress", default="", options={'SKIP_SAVE'})
    progress_current: IntProperty(default=0, options={'SKIP_SAVE'})
    progress_total: IntProperty(default=0, options={'SKIP_SAVE'})

    # Multi-action selection
    action_list: CollectionProperty(type=OMNI_ActionItem)
    action_list_index: IntProperty(default=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_frame_range(action, settings):
    """Return (frame_start, frame_end) to use for this action.

    frame_end is treated as EXCLUSIVE (matching the existing range(...) calls
    throughout the render code), so when auto-matching an Action's length we
    add 1 to its last keyframe to make sure that last frame is included.
    """
    if settings.auto_frame_range and action is not None:
        try:
            fs, fe = action.frame_range
            frame_start = int(round(fs))
            frame_end = int(round(fe)) + 1
            if frame_end <= frame_start:
                frame_end = frame_start + 1
            return frame_start, frame_end
        except Exception:
            pass
    return settings.frame_start, settings.frame_end


def compute_total_render_count(actions, settings):
    """Total number of individual frame renders across all given actions,
    used to size the progress bar/text before an export starts."""
    total = 0
    passes = 2 if settings.export_normals else 1
    for action in actions:
        frame_start, frame_end = compute_frame_range(action, settings)
        frame_count = max(0, frame_end - frame_start)
        total += frame_count * settings.num_directions * passes
    return total


def sync_action_list(settings):
    """Keep settings.action_list in sync with bpy.data.actions while
    preserving the user's checkbox selections.

    IMPORTANT: Blender does not allow writing to data-blocks from inside a
    Panel.draw() call (it's a read-only context). This function must only
    ever be called from a proper write-context: an operator's execute(),
    a timer callback, or an app handler -- never directly from draw().
    """
    current_names = [a.name for a in bpy.data.actions]
    existing_names = [item.name for item in settings.action_list]
    if existing_names == current_names:
        return
    existing_selected = {item.name: item.selected for item in settings.action_list}
    settings.action_list.clear()
    for name in current_names:
        item = settings.action_list.add()
        item.name = name
        item.selected = existing_selected.get(name, False)


def _sync_all_scenes():
    """Timer callback: syncs the action list for every scene's settings.
    Runs on Blender's main loop outside of any draw() call, so writing to
    the collection property here is allowed."""
    try:
        for scene in bpy.data.scenes:
            settings = getattr(scene, "omni_render_settings", None)
            if settings is not None:
                sync_action_list(settings)
    except Exception:
        pass
    return 1.0  # reschedule every 1 second


class ProgressTracker:
    """Tracks render progress, drives Blender's native progress cursor, and
    keeps a human readable "X of Y complete" string on the settings so the
    panel can display it."""

    def __init__(self, context, settings, total):
        self.context = context
        self.settings = settings
        self.total = total
        self.current = 0
        settings.progress_total = total
        settings.progress_current = 0
        settings.progress_text = f"0 of {total} complete" if total else "Starting..."
        context.window_manager.progress_begin(0, max(total, 1))
        self._redraw()

    def step(self):
        self.current += 1
        self.settings.progress_current = self.current
        self.settings.progress_text = f"{self.current} of {self.total} complete"
        self.context.window_manager.progress_update(self.current)
        if self.settings.show_progress:
            self._redraw()

    def _redraw(self):
        try:
            for window in self.context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        except Exception:
            pass

    def finish(self):
        self.settings.progress_text = f"Done: {self.total} of {self.total} complete" if self.total else "Done"
        self.context.window_manager.progress_end()
        self._redraw()


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class OMNI_UL_actions(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=item.name, icon='ACTION')


class OMNI_PT_panel(Panel):
    bl_label = "Omnidirectional Isometric Render"
    bl_idname = "OMNI_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'OmniIsoRender'

    def draw(self, context):
        layout = self.layout
        props = context.scene.omni_render_settings

        layout.operator("omni.setup_camera", text="Setup Camera")
        layout.separator()

        layout.prop(props, "measure_time")
        layout.prop(props, "show_progress")

        row = layout.row(align=True)
        layout.label(text="Render Resolution:")
        row = layout.row(align=True)
        row.prop(props, "resolution_x", text="X")
        row.prop(props, "resolution_y", text="Y")

        layout.label(text="Output Resolution:")
        row = layout.row(align=True)
        row.prop(props, "output_x", text="X")
        row.prop(props, "output_y", text="Y")

        layout.separator()
        layout.label(text="Animation Frames to be exported:")
        layout.prop(props, "auto_frame_range")
        row = layout.row(align=True)
        row.enabled = not props.auto_frame_range
        row.prop(props, "frame_start", text="Start")
        row.prop(props, "frame_end", text="End")

        row = layout.row(align=True)
        row.label(text="Number of Directions:")
        row.prop(props, "num_directions", text="")

        layout.prop(props, "export_format", text="Format")
        layout.prop(props, "transparent_bg")
        layout.prop(props, "export_normals")
        layout.separator()
        layout.operator("omni.export_all", text="Export All Actions")
        layout.separator()

        layout.label(text="Export only specific Action:")
        layout.prop_search(props, "selected_action", bpy.data, "actions", text="Choose Action")
        layout.operator("omni.export_specific", text="Export Specific Action")

        layout.separator()
        box = layout.box()
        box.label(text="Export Multiple Specific Actions:")
        row = box.row()
        row.template_list(
            "OMNI_UL_actions", "", props, "action_list", props, "action_list_index", rows=5
        )
        col = row.column(align=True)
        col.operator("omni.refresh_actions", text="", icon='FILE_REFRESH')
        op = col.operator("omni.select_all_actions", text="", icon='CHECKBOX_HLT')
        op.select = True
        op = col.operator("omni.select_all_actions", text="", icon='CHECKBOX_DEHLT')
        op.select = False
        box.operator("omni.export_selected", text="Export Selected Actions")

        if props.progress_text:
            layout.separator()
            pbox = layout.box()
            pbox.label(text=props.progress_text, icon='SORTTIME')


# ---------------------------------------------------------------------------
# Camera / material setup (unchanged)
# ---------------------------------------------------------------------------

class OMNI_OT_setup_camera(Operator):
    bl_idname = "omni.setup_camera"
    bl_label = "Setup Camera"

    def execute(self, context):
        empty = bpy.data.objects.new("Empty", None)
        bpy.context.collection.objects.link(empty)

        cam_data = bpy.data.cameras.new("Camera")
        cam_obj = bpy.data.objects.new("Camera", cam_data)
        cam_obj.parent = empty
        bpy.context.collection.objects.link(cam_obj)

        cam_data.type = 'ORTHO'
        cam_data.ortho_scale = 7.258
        cam_obj.location = (0, -14.82, 18.63)
        cam_obj.rotation_euler = [math.radians(38.6), 0, 0]

        light_data = bpy.data.lights.new("Light", type='POINT')
        light_data.energy = 1000
        light = bpy.data.objects.new("Light", light_data)
        light.parent = empty
        bpy.context.collection.objects.link(light)
        light.location = (0, -4, 4)

        return {'FINISHED'}

def create_normal_map_material():
    mat = bpy.data.materials.get("Normal_Map_Material")
    if mat: return mat  # reuse if already created

    mat = bpy.data.materials.new(name="Normal_Map_Material")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    nodes.clear()
    output = nodes.new(type='ShaderNodeOutputMaterial')
    emission = nodes.new(type='ShaderNodeEmission')
    geometry = nodes.new(type='ShaderNodeNewGeometry')
    vector_transform = nodes.new(type='ShaderNodeVectorTransform')

    vector_transform.inputs['Vector'].default_value = (0, 0, 0)
    vector_transform.vector_type = 'NORMAL'
    vector_transform.convert_from = 'WORLD'
    vector_transform.convert_to = 'CAMERA'

    links.new(geometry.outputs['Normal'], vector_transform.inputs['Vector'])
    links.new(vector_transform.outputs['Vector'], emission.inputs['Color'])
    links.new(emission.outputs['Emission'], output.inputs['Surface'])

    mat.use_backface_culling = True
    mat.blend_method = 'OPAQUE'

    return mat

def assign_material_to_all_meshes(mat):
    original_mats = {}
    for obj in bpy.context.scene.objects:
        if obj.type == 'MESH':
            original_mats[obj.name] = [slot.material for slot in obj.material_slots]
            obj.data.materials.clear()
            obj.data.materials.append(mat)
    return original_mats

def restore_materials(original_mats):
    for obj_name, mats in original_mats.items():
        obj = bpy.data.objects.get(obj_name)
        if obj:
            obj.data.materials.clear()
            for mat in mats:
                obj.data.materials.append(mat)

def bake_simulations(scene, armature_obj):
    def is_child_of(obj, parent):
        while obj.parent:
            if obj.parent == parent:
                return True
            obj = obj.parent
        return False

    for obj in scene.objects:
        if obj.type != 'MESH' or not is_child_of(obj, armature_obj):
            continue

        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)

        baked = False

        # others using point_cache
        for mod in obj.modifiers:
            if hasattr(mod, 'point_cache'):
                mod.show_viewport = True
                mod.show_render = True
                try:
                    bpy.ops.ptcache.free_bake_all()
                    bpy.ops.ptcache.bake_all(bake=True)
                    print(f"Baked (ptcache): {obj.name}")
                    baked = True
                except Exception as e:
                    print(f"Failed ptcache bake on {obj.name}: {e}")
                break  # Don't re-bake the same object again

        # Specific bake for cloth (more reliable in some cases)
        if any(m.type == 'CLOTH' for m in obj.modifiers):
            try:
                bpy.ops.object.bake(type='CLOTH')
                print(f"Baked (cloth): {obj.name}")
                baked = True
            except Exception as e:
                print(f"Failed cloth bake on {obj.name}: {e}")

        # Soft body
        if any(m.type == 'SOFT_BODY' for m in obj.modifiers):
            try:
                bpy.ops.object.bake(type='SOFTBODY')
                print(f"Baked (soft body): {obj.name}")
                baked = True
            except Exception as e:
                print(f"Failed soft body bake on {obj.name}: {e}")

        # Rigid body cache (not in modifiers)
        if obj.rigid_body:
            try:
                bpy.ops.rigidbody.bake_to_keyframes()
                print(f"Baked (rigid body): {obj.name}")
                baked = True
            except Exception as e:
                print(f"Failed rigid body bake on {obj.name}: {e}")

        if not baked:
            print(f"No bakeable sims found on: {obj.name}")

        obj.select_set(False)


# ---------------------------------------------------------------------------
# Core render function
# ---------------------------------------------------------------------------

def render_spin(path, rig, action_name, settings, normal_pass=False,
                 frame_start=None, frame_end=None, progress=None):
    scene = bpy.context.scene

    if frame_start is None:
        frame_start = settings.frame_start
    if frame_end is None:
        frame_end = settings.frame_end

    num_directions = settings.num_directions
    export_format = settings.export_format

    width, height = settings.resolution_x, settings.resolution_y
    output_width, output_height = settings.output_x, settings.output_y

    scene.render.resolution_x = width
    scene.render.resolution_y = height

    rotate_circle = bpy.data.objects['Empty']
    step_rotation = 360 / num_directions

    log_lines = []
    render_times = []
    process_times = []
    frame_switch_times = []
    save_times = []
    total_frames = 0
    total_start = time.time()

    def record_timing(s0, t0, t1, t2, t3=None):
        frame_switch_times.append(t0 - s0)
        render_times.append(t1 - t0)
        process_times.append(t2 - t1)
        if t3:
            save_times.append(t3 - t2)

    suffix = "_normal" if normal_pass else ""

    if normal_pass and settings.export_normals:
        normal_mat = create_normal_map_material()
        originals = assign_material_to_all_meshes(normal_mat)
    else:
        normal_mat = None

    if not normal_pass:
        sim0 = time.time()
        scene.frame_set(0)
        for f in range(frame_start, frame_end + 1):
            scene.frame_set(f)
            bpy.context.view_layer.update()
        sim1 = time.time()

        bake0 = time.time()
        bake_simulations(scene, rig)
        bake1 = time.time()


    if export_format == "Single":
        for x in range(num_directions):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x

            for frame in range(frame_start, frame_end):
                s0 = time.time()
                scene.frame_set(frame)
                output_dir = os.path.join(path, f"{action_name}{suffix}", str(x))
                os.makedirs(output_dir, exist_ok=True)
                temp_file = os.path.join(tempfile.gettempdir(), f"{x}_{frame}.png")
                scene.render.filepath = temp_file

                t0 = time.time()
                bpy.ops.render.render(animation=False, write_still=True)
                t1 = time.time()

                img = Image.open(temp_file)
                img = img.resize((output_width, output_height), resample=Image.NEAREST)
                t2 = time.time()

                img.save(os.path.join(output_dir, f"{frame}.png"))
                t3 = time.time()

                total_frames += 1

                if progress:
                    progress.step()

                if settings.measure_time:
                    record_timing(s0, t0, t1, t2, t3)
                    log_lines.append(
                        f"Action: {action_name} | Dir: {x} | Frame: {frame} | "
                        f"Switch: {t0 - s0:.4f}s | Render: {t1 - t0:.4f}s | Process: {t2 - t1:.4f}s"
                    )

    elif export_format == "Spritesheet":
        images = []
        temp_dir = tempfile.mkdtemp()

        for x in range(num_directions):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x

            for frame in range(frame_start, frame_end):
                s0 = time.time()
                scene.frame_set(frame)
                filepath = os.path.join(temp_dir, f"{x}_{frame}.png")
                scene.render.filepath = filepath

                t0 = time.time()
                bpy.ops.render.render(animation=False, write_still=True)
                t1 = time.time()

                img = Image.open(filepath)
                img = img.resize((output_width, output_height), resample=Image.NEAREST)
                t2 = time.time()

                images.append((x, frame, img))

                total_frames += 1

                if progress:
                    progress.step()

                if settings.measure_time:
                    record_timing(s0, t0, t1, t2)
                    log_lines.append(
                        f"Action: {action_name} | Dir: {x} | Frame: {frame} | "
                        f"Switch: {t0 - s0:.4f}s | Render: {t1 - t0:.4f}s | Process: {t2 - t1:.4f}s"
                    )

        sheet = Image.new("RGBA", (output_width * (frame_end - frame_start), output_height * num_directions))
        for x, frame, img in images:
            sheet.paste(img, (output_width * (frame - frame_start), output_height * x))

        t3 = time.time()
        sheet.save(os.path.join(path, f"{action_name}{suffix}.png"))
        t4 = time.time()

        if settings.measure_time:
            save_times.append(t4 - t3)
            log_lines.append(f"Spritesheet Save Time: {t4 - t3:.4f}s")

    elif export_format == "Spritesheet_DIR":
        for x in range(num_directions):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x
            images = []
            temp_dir = tempfile.mkdtemp()

            for frame in range(frame_start, frame_end):
                s0 = time.time()
                scene.frame_set(frame)
                filepath = os.path.join(temp_dir, f"{x}_{frame}.png")
                scene.render.image_settings.color_mode = 'RGBA' if settings.transparent_bg else 'RGB'
                scene.render.film_transparent = settings.transparent_bg
                scene.render.filepath = filepath

                t0 = time.time()
                bpy.ops.render.render(animation=False, write_still=True)
                t1 = time.time()

                img = Image.open(filepath).convert("RGBA" if settings.transparent_bg else "RGB")
                img = img.resize((output_width, output_height), resample=Image.NEAREST)
                t2 = time.time()

                images.append((frame, img))

                total_frames += 1

                if progress:
                    progress.step()

                if settings.measure_time:
                    record_timing(s0, t0, t1, t2)
                    log_lines.append(
                        f"Action: {action_name} | Dir: {x} | Frame: {frame} | "
                        f"Switch: {t0 - s0:.4f}s | Render: {t1 - t0:.4f}s | Process: {t2 - t1:.4f}s"
                    )

            if images:
                frame_count = frame_end - frame_start
                mode = "RGBA" if settings.transparent_bg else "RGB"
                sheet = Image.new(mode, (output_width * frame_count, output_height))
                for frame, img in images:
                    sheet.paste(img, (output_width * (frame - frame_start), 0))

                t3 = time.time()
                sheet.save(os.path.join(path, f"{action_name}{suffix}_{x}.png"))
                t4 = time.time()

                if settings.measure_time:
                    save_times.append(t4 - t3)
                    log_lines.append(f"Spritesheet Save Time (Dir {x}): {t4 - t3:.4f}s")

    elif export_format == "GIF_DIR":
        for x in range(num_directions):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x
            frames = []

            for frame in range(frame_start, frame_end):
                s0 = time.time()
                scene.frame_set(frame)
                temp_file = os.path.join(tempfile.gettempdir(), f"{x}_{frame}.png")
                scene.render.image_settings.color_mode = 'RGBA' if settings.transparent_bg else 'RGB'
                scene.render.film_transparent = settings.transparent_bg
                scene.render.filepath = temp_file

                t0 = time.time()
                bpy.ops.render.render(animation=False, write_still=True)
                t1 = time.time()

                img = Image.open(temp_file).convert("RGBA" if settings.transparent_bg else "RGB")
                img = img.resize((output_width, output_height), resample=Image.NEAREST)
                t2 = time.time()

                frames.append(img)

                total_frames += 1

                if progress:
                    progress.step()

                if settings.measure_time:
                    record_timing(s0, t0, t1, t2)
                    log_lines.append(
                        f"Action: {action_name} | Dir: {x} | Frame: {frame} | "
                        f"Switch: {t0 - s0:.4f}s | Render: {t1 - t0:.4f}s | Process: {t2 - t1:.4f}s"
                    )

            if frames:
                gif_path = os.path.join(path, f"{action_name}{suffix}_{x}.gif")
                t3 = time.time()
                frames[0].save(gif_path, save_all=True, append_images=frames[1:], optimize=False, duration=1, loop=0, transparency=0, disposal=2)
                t4 = time.time()

                if settings.measure_time:
                    save_times.append(t4 - t3)
                    log_lines.append(f"GIF Save Time (Dir {x}): {t4 - t3:.4f}s")

    elif export_format == "GIF_ONE":
        all_frames = []

        for x in range(num_directions):
            rotate_circle.rotation_euler[2] = math.radians(-90) + math.radians(step_rotation) * x

            for frame in range(frame_start, frame_end):
                s0 = time.time()
                scene.frame_set(frame)
                temp_file = os.path.join(tempfile.gettempdir(), f"{x}_{frame}.png")
                scene.render.image_settings.color_mode = 'RGBA' if settings.transparent_bg else 'RGB'
                scene.render.film_transparent = settings.transparent_bg
                scene.render.filepath = temp_file

                t0 = time.time()
                bpy.ops.render.render(animation=False, write_still=True)
                t1 = time.time()

                img = Image.open(temp_file).convert("RGBA" if settings.transparent_bg else "RGB")
                img = img.resize((output_width, output_height), resample=Image.NEAREST)
                t2 = time.time()

                all_frames.append(img)

                total_frames += 1

                if progress:
                    progress.step()

                if settings.measure_time:
                    record_timing(s0, t0, t1, t2)
                    log_lines.append(
                        f"Action: {action_name} | Dir: {x} | Frame: {frame} | "
                        f"Switch: {t0 - s0:.4f}s | Render: {t1 - t0:.4f}s | Process: {t2 - t1:.4f}s"
                    )

        if all_frames:
            gif_path = os.path.join(path, f"{action_name}{suffix}_combined.gif")
            t3 = time.time()
            all_frames[0].save(gif_path, save_all=True, append_images=all_frames[1:], optimize=False, duration=1, loop=0, transparency=0, disposal=2)
            t4 = time.time()

            save_times.append(t4 - t3)
            log_lines.append(f"Combined GIF Save Time: {t4 - t3:.4f}s")

    rotate_circle.rotation_euler[2] = 0
    total_end = time.time()

    if normal_pass and settings.export_normals:
        restore_materials(originals)

    if settings.measure_time:
        log_lines.append("")
        log_lines.append(f"Total Frames: {total_frames}")
        try:
            log_lines.append(f"Simulation Update: {sim1 - sim0:.4f} sec")
            log_lines.append(f"Baking: {bake1 - bake0:.4f} sec")
        except:
            pass
        log_lines.append(f"Frame Switch Time: {sum(frame_switch_times):.4f}s | Avg: {statistics.mean(frame_switch_times):.4f}s")
        log_lines.append(f"Total Render Time: {sum(render_times):.4f}s | Avg: {statistics.mean(render_times):.4f}s")
        log_lines.append(f"Total Process Time: {sum(process_times):.4f}s | Avg: {statistics.mean(process_times):.4f}s")
        if save_times:
            log_lines.append(f"Total Save Time: {sum(save_times):.4f}s | Avg: {statistics.mean(save_times):.4f}s")
        log_lines.append(f"TOTAL TIME for {action_name}: {total_end - total_start:.4f} sec")

        log_path = os.path.join(path, f"{action_name}{suffix}_render_time_log.txt")
        with open(log_path, 'w') as f:
            f.write("\n".join(log_lines))


# ---------------------------------------------------------------------------
# Batch export functions
# ---------------------------------------------------------------------------

def render_all_actions(path, rig, settings, progress=None):
    if rig not in bpy.data.objects or bpy.data.objects[rig].type != 'ARMATURE':
        raise Exception("Select a valid Armature")

    armature = bpy.data.objects[rig]
    render_root = os.path.join(path, "Render")
    os.makedirs(render_root, exist_ok=True)

    for action in bpy.data.actions:
        armature.animation_data.action = action
        frame_start, frame_end = compute_frame_range(action, settings)
        render_spin(render_root, armature, action.name, settings, normal_pass=False,
                    frame_start=frame_start, frame_end=frame_end, progress=progress)  # Normal render with baking
        if settings.export_normals:
            render_spin(render_root, armature, action.name, settings, normal_pass=True,
                        frame_start=frame_start, frame_end=frame_end, progress=progress)  # Normal maps without baking

def render_single_action(path, rig, settings, action_name, progress=None):
    if rig not in bpy.data.objects or bpy.data.objects[rig].type != 'ARMATURE':
        raise Exception("Select a valid Armature")

    armature = bpy.data.objects[rig]
    render_root = os.path.join(path, "Render")
    os.makedirs(render_root, exist_ok=True)

    action = bpy.data.actions.get(action_name)
    if action:
        armature.animation_data.action = action
        frame_start, frame_end = compute_frame_range(action, settings)
        render_spin(render_root, armature, action.name, settings, normal_pass=False,
                    frame_start=frame_start, frame_end=frame_end, progress=progress)  # Normal render with baking
        if settings.export_normals:
            render_spin(render_root, armature, action.name, settings, normal_pass=True,
                        frame_start=frame_start, frame_end=frame_end, progress=progress)  # Normal maps without baking

def render_selected_actions(path, rig, settings, action_names, progress=None):
    if rig not in bpy.data.objects or bpy.data.objects[rig].type != 'ARMATURE':
        raise Exception("Select a valid Armature")

    armature = bpy.data.objects[rig]
    render_root = os.path.join(path, "Render")
    os.makedirs(render_root, exist_ok=True)

    for action_name in action_names:
        action = bpy.data.actions.get(action_name)
        if not action:
            continue
        armature.animation_data.action = action
        frame_start, frame_end = compute_frame_range(action, settings)
        render_spin(render_root, armature, action.name, settings, normal_pass=False,
                    frame_start=frame_start, frame_end=frame_end, progress=progress)  # Normal render with baking
        if settings.export_normals:
            render_spin(render_root, armature, action.name, settings, normal_pass=True,
                        frame_start=frame_start, frame_end=frame_end, progress=progress)  # Normal maps without baking


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _get_active_rig_name(context, operator):
    active = bpy.context.active_object
    if active and active.type == 'ARMATURE':
        return active.name
    operator.report({'ERROR'}, "Select an armature first.")
    return None


class OMNI_OT_export_all(Operator):
    bl_idname = "omni.export_all"
    bl_label = "Export All Actions"

    def execute(self, context):
        settings = context.scene.omni_render_settings
        base_path = os.path.dirname(bpy.data.filepath)
        rig = _get_active_rig_name(context, self)
        if not rig:
            return {'CANCELLED'}

        actions = list(bpy.data.actions)
        total = compute_total_render_count(actions, settings)
        progress = ProgressTracker(context, settings, total)

        try:
            render_all_actions(base_path, rig, settings, progress=progress)
        except Exception as e:
            progress.finish()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        progress.finish()
        self.report({'INFO'}, f"Export complete: {total} frames rendered.")
        return {'FINISHED'}

class OMNI_OT_export_specific(Operator):
    bl_idname = "omni.export_specific"
    bl_label = "Export Specific Action"

    def execute(self, context):
        settings = context.scene.omni_render_settings
        base_path = os.path.dirname(bpy.data.filepath)
        rig = _get_active_rig_name(context, self)
        if not rig:
            return {'CANCELLED'}

        if not settings.selected_action:
            self.report({'ERROR'}, "No action selected.")
            return {'CANCELLED'}

        action = bpy.data.actions.get(settings.selected_action)
        total = compute_total_render_count([action] if action else [], settings)
        progress = ProgressTracker(context, settings, total)

        try:
            render_single_action(base_path, rig, settings, settings.selected_action, progress=progress)
        except Exception as e:
            progress.finish()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        progress.finish()
        self.report({'INFO'}, f"Export complete: {total} frames rendered.")
        return {'FINISHED'}

class OMNI_OT_refresh_actions(Operator):
    bl_idname = "omni.refresh_actions"
    bl_label = "Refresh Action List"
    bl_description = "Refresh the list to include any newly added/renamed/removed Actions"

    def execute(self, context):
        settings = context.scene.omni_render_settings
        sync_action_list(settings)
        return {'FINISHED'}

class OMNI_OT_select_all_actions(Operator):
    bl_idname = "omni.select_all_actions"
    bl_label = "Select/Deselect All Actions"
    select: BoolProperty(default=True)

    def execute(self, context):
        settings = context.scene.omni_render_settings
        for item in settings.action_list:
            item.selected = self.select
        return {'FINISHED'}

class OMNI_OT_export_selected(Operator):
    bl_idname = "omni.export_selected"
    bl_label = "Export Selected Actions"

    def execute(self, context):
        settings = context.scene.omni_render_settings
        base_path = os.path.dirname(bpy.data.filepath)
        rig = _get_active_rig_name(context, self)
        if not rig:
            return {'CANCELLED'}

        action_names = [item.name for item in settings.action_list if item.selected]
        if not action_names:
            self.report({'ERROR'}, "No actions selected. Check the boxes next to the actions you want to export.")
            return {'CANCELLED'}

        actions = [bpy.data.actions.get(n) for n in action_names]
        actions = [a for a in actions if a is not None]
        total = compute_total_render_count(actions, settings)
        progress = ProgressTracker(context, settings, total)

        try:
            render_selected_actions(base_path, rig, settings, action_names, progress=progress)
        except Exception as e:
            progress.finish()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        progress.finish()
        self.report({'INFO'}, f"Export complete: {total} frames rendered across {len(action_names)} action(s).")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
    OMNI_ActionItem,
    OmniRenderSettings,
    OMNI_UL_actions,
    OMNI_PT_panel,
    OMNI_OT_setup_camera,
    OMNI_OT_export_all,
    OMNI_OT_export_specific,
    OMNI_OT_refresh_actions,
    OMNI_OT_select_all_actions,
    OMNI_OT_export_selected,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.omni_render_settings = PointerProperty(type=OmniRenderSettings)

    if not bpy.app.timers.is_registered(_sync_all_scenes):
        bpy.app.timers.register(_sync_all_scenes, first_interval=1.0)

def unregister():
    if bpy.app.timers.is_registered(_sync_all_scenes):
        bpy.app.timers.unregister(_sync_all_scenes)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.omni_render_settings

if __name__ == "__main__":
    register()
