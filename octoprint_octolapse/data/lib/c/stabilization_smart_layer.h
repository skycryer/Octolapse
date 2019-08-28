#ifndef StabilizationSmartLayer_H
#define StabilizationSmartLayer_H
#include "stabilization.h"
#include "position.h"
#include "trigger_position.h"
#ifdef _DEBUG
#undef _DEBUG
#include <Python.h>
#define _DEBUG
#endif
static const char* SMART_LAYER_STABILIZATION = "smart_layer";



struct smart_layer_args
{
	smart_layer_args()
	{
		smart_layer_trigger_type = trigger_position::trigger_type::compatibility;
		speed_threshold = 0;
		snap_to_fastest = false;
	}
	trigger_position::trigger_type smart_layer_trigger_type;
	double speed_threshold;
	bool snap_to_fastest;
};

class stabilization_smart_layer : public stabilization
{
public:
	stabilization_smart_layer();
	stabilization_smart_layer(gcode_position_args* position_args, stabilization_args* stab_args, smart_layer_args* mt_args, progressCallback progress);
	stabilization_smart_layer(gcode_position_args* position_args, stabilization_args* stab_args, smart_layer_args* mt_args, pythonGetCoordinatesCallback get_coordinates,  pythonProgressCallback progress);
	~stabilization_smart_layer();
private:
	stabilization_smart_layer(const stabilization_smart_layer &source); // don't copy me
	void process_pos(position& p_current_pos, position& p_previous_pos) override;
	void on_processing_complete() override;
	void add_plan();
	void reset_saved_positions();
	/**
	 * \brief Determine if a position is closer.  If necessary, filter based on speed, and also detect 
	 * if there are multiple extrusion speeds if necessary.
	 * previous points, or -1 (less than 0) if it is not.
	 * \param p_position the position to test
	 * \param type_ the type of position we are comparing, either extrusion or retracted travel
	 * \param distance the distance between the supplied position and the stabilization point.  Is set to -1 if there are errors
	 * \return true if the position is closer, false if it is not or if it is filtered
	 */
	bool is_closer(position* p_position, trigger_position::position_type type_, double &distance);
	void update_stabilization_coordinates();
	// Layer/height tracking variables
	bool is_layer_change_wait_;
	int current_layer_;
	int last_tested_gcode_number_;
	double fastest_extrusion_speed_;
	double slowest_extrusion_speed_;
	bool has_one_extrusion_speed_;
	unsigned int current_height_increment_;
	double stabilization_x_;
	double stabilization_y_;
	double current_layer_saved_extrusion_speed_;
	double standard_layer_trigger_distance_;
	smart_layer_args *p_smart_layer_args_;
	// closest extrusion/travel position tracking variables
	trigger_positions closest_positions_;
};
#endif