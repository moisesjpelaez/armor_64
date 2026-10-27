#include <libdragon.h>
#include <stdint.h>
#include <t3d/t3d.h>
#include <t3d/t3dmath.h>
#include <t3d/t3dmodel.h>

#include "engine.h"
#include "iron/object/animation.h"
#include "iron/system/input.h"
#include "types.h"
#include "utils.h"

#if ENGINE_ENABLE_PHYSICS
#include "oimo/physics.h"
#if ENGINE_ENABLE_PHYSICS_DEBUG
#include "oimo/debug/physics_debug.h"
#endif
#endif

static int frameIdx = 0;

// Forward declaration — called in renderer_begin_frame before camera setup
void renderer_update_objects(ArmScene *scene);

void renderer_begin_frame(T3DViewport *viewport, ArmScene *scene) {
	// Update object matrices FIRST — camera parent-follow needs the parent's world_mat
	frameIdx = (frameIdx + 1) % FB_COUNT;
	renderer_update_objects(scene);

  	ArmCamera *cam = &scene->cameras[scene->active_camera_id];

	// If camera has a parent, transform local pos/target into world space
	if (cam->parent_world_mat) {
		T3DVec4 world_pos4, world_target4;
		t3d_mat4_mul_vec3(&world_pos4, cam->parent_world_mat, &cam->local_pos);
		t3d_mat4_mul_vec3(&world_target4, cam->parent_world_mat, &cam->local_target);
		// Scale back to 1x space for consistency within the camera struct
		cam->transform.loc = (T3DVec3){{world_pos4.v[0] * WORLD_SCALE_FACTOR, world_pos4.v[1] * WORLD_SCALE_FACTOR, world_pos4.v[2] * WORLD_SCALE_FACTOR}};
		cam->target = (T3DVec3){{world_target4.v[0] * WORLD_SCALE_FACTOR, world_target4.v[1] * WORLD_SCALE_FACTOR, world_target4.v[2] * WORLD_SCALE_FACTOR}};
	} else {
		// Moved by a trait (transform.loc): same rotation, as in Armory
		t3d_vec3_add(&cam->target, &cam->transform.loc, &cam->look_dir);
	}

	T3DVec3 cam_loc_scaled = (T3DVec3){{cam->transform.loc.v[0] * WORLD_SCALE_FACTOR_INV, cam->transform.loc.v[1] * WORLD_SCALE_FACTOR_INV, cam->transform.loc.v[2] * WORLD_SCALE_FACTOR_INV}};
	T3DVec3 cam_target_scaled = (T3DVec3){{cam->target.v[0] * WORLD_SCALE_FACTOR_INV, cam->target.v[1] * WORLD_SCALE_FACTOR_INV, cam->target.v[2] * WORLD_SCALE_FACTOR_INV}};

	// Real near/far: tiny3d normalizes depth (W) from them, so the matrix must not be rescaled afterwards
  	t3d_viewport_set_projection(viewport, T3D_DEG_TO_RAD(cam->fov), cam->near * WORLD_SCALE_FACTOR_INV, cam->far * WORLD_SCALE_FACTOR_INV);

	// A camera beyond ±511 BU can't be converted to fixed point: keep the last view
	bool camera_fits = true;
	for (int i = 0; i < 3; i++) {
		camera_fits &= fits_fixed_point(cam_loc_scaled.v[i]) && fits_fixed_point(cam_target_scaled.v[i]);
	}
	if (camera_fits) {
	  	t3d_viewport_look_at(viewport, &cam_loc_scaled, &cam_target_scaled, &(T3DVec3){{0.0f, 1.0f, 0.0f}});
	}
}

// The object's float world matrix (×64 space), composed with its parent's
static void build_world_matrix(ArmScene *scene, ArmObject *obj) {
	if (obj->parent_index >= 0) {
		T3DMat4 local;
		t3d_mat4_from_srt(&local, obj->transform.scale.v, obj->transform.rot.v, obj->transform.loc.v);
		t3d_mat4_mul(&obj->world_mat, &scene->objects[obj->parent_index].world_mat, &local);
	} else {
		// Roots hold Blender-unit positions and 1/64 scales: bring both to the ×64 space
		float scale[3] = {
			obj->transform.scale.v[0] * WORLD_SCALE_FACTOR_INV,
			obj->transform.scale.v[1] * WORLD_SCALE_FACTOR_INV,
			obj->transform.scale.v[2] * WORLD_SCALE_FACTOR_INV
		};
		float loc[3] = {
			obj->transform.loc.v[0] * WORLD_SCALE_FACTOR_INV,
			obj->transform.loc.v[1] * WORLD_SCALE_FACTOR_INV,
			obj->transform.loc.v[2] * WORLD_SCALE_FACTOR_INV
		};
		t3d_mat4_from_srt(&obj->world_mat, scale, obj->transform.rot.v, loc);
	}
}

static bool matrix_fits_fixed_point(const T3DMat4 *m) {
	const float *f = &m->m[0][0];
	for (int i = 0; i < 16; i++) {
		if (!fits_fixed_point(f[i])) return false;
	}
	return true;
}

// World AABB of the local bounds through the full world matrix (rotation, scale and
// parents; Arvo's method), in the ×64 space of the camera frustum
static void update_world_aabb(ArmObject *obj) {
	const T3DMat4 *m = &obj->world_mat;
	for (int j = 0; j < 3; j++) {
		float center = m->m[3][j];
		float extent = 0.0f;
		for (int k = 0; k < 3; k++) {
			center += m->m[k][j] * obj->bounds_center.v[k];
			extent += fabsf(m->m[k][j]) * obj->bounds_half.v[k];
		}
		obj->cached_world_aabb_min.v[j] = center - extent;
		obj->cached_world_aabb_max.v[j] = center + extent;
	}
}

void renderer_update_objects(ArmScene *scene) {
	// A child is rebuilt in the frame its parent changes. Parents come before
	// their children (sorted at export), so one pass covers any depth.
	if (scene->has_parented_objects) {
		for (uint16_t i = 0; i < scene->object_count; i++) {
			ArmObject *obj = &scene->objects[i];
			if (obj->is_removed || obj->parent_index < 0) continue;
			if (scene->objects[obj->parent_index].transform.dirty == TRANSFORM_DIRTY_FRAMES) {
				obj->transform.dirty = TRANSFORM_DIRTY_FRAMES;
			}
		}
	}

	for (uint16_t i = 0; i < scene->object_count; i++) {
		ArmObject *obj = &scene->objects[i];
		if (obj->is_removed || obj->transform.dirty == 0) continue;

		// Changed this frame: rebuild the float matrix and the culling box. The next
		// frames only convert the same matrix into their own slot.
		if (obj->transform.dirty == TRANSFORM_DIRTY_FRAMES) {
			build_world_matrix(scene, obj);
			bool was_valid = obj->matrix_valid;
			obj->matrix_valid = matrix_fits_fixed_point(&obj->world_mat);
			if (obj->matrix_valid) {
				update_world_aabb(obj);
			} else if (was_valid) {
				debugf("Object %u is beyond the drawable range (±511 BU) or not a number: not drawn\n", i);
			}
		}

		if (obj->model_mat && obj->matrix_valid) {
			t3d_mat4_to_fixed(&obj->model_mat[frameIdx], &obj->world_mat);
		}
		obj->transform.dirty--;
	}
}

void renderer_draw_scene(T3DViewport *viewport, ArmScene *scene) {
	// Objects already updated in renderer_begin_frame

	surface_t *fb = display_get();
	rdpq_attach(fb, display_get_zbuf());
	t3d_frame_start();
	t3d_viewport_attach(viewport);

	t3d_screen_clear_color(RGBA32(scene->world.clear_color[0], scene->world.clear_color[1], scene->world.clear_color[2], scene->world.clear_color[3]));
	t3d_screen_clear_depth();

	t3d_state_set_drawflags(T3D_FLAG_DEPTH | T3D_FLAG_CULL_BACK);

	t3d_light_set_ambient(scene->world.ambient_color);
	t3d_light_set_count(scene->light_count);

	for (uint8_t i = 0; i < scene->light_count; i++) {
		ArmLight *l = &scene->lights[i];
		t3d_light_set_directional(i, l->color, &l->dir);
	}

	const T3DFrustum *frustum = &viewport->viewFrustum;
	int visible_count = 0;

	// Render dynamic objects with per-frame frustum culling
	t3d_matrix_push_pos(1);
	for (uint16_t i = 0; i < scene->object_count; i++) {
		ArmObject *obj = &scene->objects[i];
		// Empties and models that failed to load have no display list
		if (!obj->visible || !obj->matrix_valid || !obj->dpl) {
			continue;
		}

		// Cached world AABB: rebuilt only when the transform changes
		if (!t3d_frustum_vs_aabb(frustum, &obj->cached_world_aabb_min, &obj->cached_world_aabb_max)) {
			continue;
		}

		visible_count++;

		// Skinned models: select skeleton buffer before drawing
		if (obj->animation) {
			animation_use(obj->animation);
		}

		t3d_matrix_set(&obj->model_mat[frameIdx], true);
		rspq_block_run(obj->dpl);
	}
	t3d_matrix_pop(1);

#ifdef ARM_DEBUG_HUD
	rdpq_sync_pipe();
	rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 200, 220, "FPS: %.2f", display_get_fps());
	rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 200, 230, "Obj: %d/%d", visible_count, scene->object_count);
#if ENGINE_ENABLE_PHYSICS
	{
		float phys_ms;
		int phys_steps, phys_tests, phys_contacts;
		physics_debug_frame(&phys_ms, &phys_steps, &phys_tests, &phys_contacts);
		rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 10, 210, "Phys: %.2f ms (%d steps)", phys_ms, phys_steps);
		rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 10, 220, "Tests: %d  Contacts: %d", phys_tests, phys_contacts);
	}
#endif

	// Input debug
	rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 10, 10, "Stick: %.2f, %.2f", input_stick_x(), input_stick_y());
	rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 10, 20, "A:%d B:%d Z:%d Start:%d", input_down(N64_BTN_A), input_down(N64_BTN_B), input_down(N64_BTN_Z), input_down(N64_BTN_START));
	rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 10, 30, "D: %d%d%d%d  C: %d%d%d%d", input_down(N64_BTN_DUP), input_down(N64_BTN_DDOWN), input_down(N64_BTN_DLEFT), input_down(N64_BTN_DRIGHT), input_down(N64_BTN_CUP), input_down(N64_BTN_CDOWN), input_down(N64_BTN_CLEFT), input_down(N64_BTN_CRIGHT));
	rdpq_text_printf(NULL, FONT_BUILTIN_DEBUG_MONO, 10, 40, "L:%d R:%d", input_down(N64_BTN_L), input_down(N64_BTN_R));
#endif
}

void renderer_end_frame(T3DViewport *viewport) {
#if ENGINE_ENABLE_PHYSICS && ENGINE_ENABLE_PHYSICS_DEBUG
	// Draw physics debug using RDP hardware (while still attached)
	physics_debug_draw(viewport, physics_get_world());
#endif
	rdpq_detach_show();
}
