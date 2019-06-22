////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
// Octolapse - A plugin for OctoPrint used for making stabilized timelapse videos.
// Copyright(C) 2019  Brad Hochgesang
////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
// This program is free software : you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.If not, see the following :
// https ://github.com/FormerLurker/Octolapse/blob/master/LICENSE
//
// You can contact the author either through the git - hub repository, or at the
// following email address : FormerLurker@pm.me
////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

#ifndef STABILIZATION_H
#define STABILIZATION_H
#include <string>
#include "position.h"
#include "gcode_position.h"
#include "snapshot_plan.h"
#include "stabilization_results.h"

#ifdef _DEBUG
#undef _DEBUG
#include <Python.h>
#define _DEBUG
#else
#include <Python.h>
#endif
#include <vector>
static const char* travel_action = "travel";
static const char* snapshot_action = "snapshot";
static const char* send_parsed_command_first = "first";
static const char* send_parsed_command_last = "last";
static const char* send_parsed_command_never = "never";

class stabilization_args {
public:
	stabilization_args()
	{
		stabilization_type = "";
		height_increment = 0.0;
		notification_period_seconds = 0.25;
		file_path = "";
		py_get_snapshot_position_callback = NULL;
		py_gcode_generator = NULL;
		py_on_progress_received = NULL;
		x_coordinate = 0;
		y_coordinate = 0;
		x_stabilization_disabled = false;
		y_stabilization_disabled = false;
	}
	~stabilization_args()
	{
		if (py_get_snapshot_position_callback != NULL)
			Py_XDECREF(py_get_snapshot_position_callback);
		if (py_gcode_generator != NULL)
			Py_XDECREF(py_gcode_generator);
		if (py_on_progress_received != NULL)
			Py_XDECREF(py_on_progress_received);
	}
	PyObject* py_on_progress_received;
	PyObject * py_get_snapshot_position_callback;
	PyObject * py_gcode_generator;
	std::string stabilization_type;
	std::string file_path;
	double height_increment;
	double notification_period_seconds;
	
	/**
	 * \brief If true, the x axis will stabilize at the layer change point.
	 */
	bool x_stabilization_disabled;
	/**
	 * \brief If true, the y axis will stabilize at the layer change point.
	 */
	bool y_stabilization_disabled;

	double x_coordinate;
	double y_coordinate;
};
typedef bool(*progressCallback)(double percentComplete, double seconds_elapsed, double estimatedSecondsRemaining, long gcodesProcessed, long linesProcessed);
typedef bool(*pythonProgressCallback)(PyObject* python_progress_callback, double percentComplete, double seconds_elapsed, double estimatedSecondsRemaining, long gcodesProcessed, long linesProcessed);
typedef bool(*pythonGetCoordinatesCallback)(PyObject* py_get_snapshot_position_callback, double x_initial, double y_initial, double* x_result, double* y_result);

class stabilization
{
public:

	stabilization();
	// constructor for use when running natively
	stabilization(gcode_position_args* position_args, stabilization_args* args, progressCallback progress);
	// constructor for use when being called from python
	stabilization(gcode_position_args* position_args, stabilization_args* args, pythonGetCoordinatesCallback get_coordinates, pythonProgressCallback progress);
	virtual ~stabilization();
	void process_file(stabilization_results* results);
	
private:
	stabilization(const stabilization &source); // don't copy me!
	double get_next_update_time() const;
	static double get_time_elapsed(double start_clock, double end_clock);
	bool has_python_callbacks_;
	// False if return < 0, else true
	pythonGetCoordinatesCallback _get_coordinates_callback;
	void notify_progress(double percent_progress, double seconds_elapsed, double seconds_to_complete,
		long gcodes_processed, long lines_processed);
	gcode_position_args* p_args_;
	// current stabilization point

	double stabilization_x_;
	double stabilization_y_;
protected:
	/**
	 * \brief Gets the next xy stabilization point
	 * \param x The current x stabilization point, will be replaced with the next x point.
	 * \param y The current y stabilization point, will be replaced with the next y point
	 */
	void get_next_xy_coordinates(double *x, double *y);
	virtual void process_pos(position* p_current_pos, position* p_previous_pos);
	virtual void on_processing_complete();
	std::vector<snapshot_plan*>* p_snapshot_plans_;
	bool is_running_;
	std::string errors_;
	stabilization_args* p_stabilization_args_;
	progressCallback native_progress_callback_;
	pythonProgressCallback progress_callback_;
	gcode_position* gcode_position_;
	gcode_parser* gcode_parser_;
	long get_file_size(const std::string& file_path);
	long file_size_;
	long lines_processed_;
	long gcodes_processed_;

	
};
#endif