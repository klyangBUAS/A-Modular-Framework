# A-Modular-Framework
Expanding Digital Physics Experiments for Student Inquiry with Generative AI

Expanding Digital Physics Experiments for Student Inquiry with Generative AI: A Modular Framework


S1. Framework Overview and Source Files
This supplementary material provides the files and procedures needed to reproduce the modular digital-laboratory framework described in the main text. The framework uses a Raspberry Pi Pico W to acquire sensor data and serves a browser-based interface for displaying measurements, plotting graphs, and exporting data.
The software separates shared components from sensor-specific functions. The shared program structure manages data acquisition, processing, and communication with the interface. Sensor-specific code initializes the hardware, reads measurements, applies calibration or conversion procedures, and supplies data in the format expected by the interface.
The repository contains the following resources:
File or resource	Purpose
main.py template	Shared MicroPython program structure for data acquisition and processing
index.html template	Browser-based interface for displaying measurements, plotting graphs, and exporting data
HC-SR04 module	Distance measurement using an ultrasonic sensor
HX711 module	Force measurement using an HX711-based load-cell system
BH1750 module	Light-intensity measurement using a digital light sensor
Wiring information	Connections required for each implemented configuration
AI adaptation prompt	Reusable instructions for adapting the software to a new sensor
Verification checklist	Checks to complete before using an adapted module in class
Repository: [Insert public GitHub URL]
The templates should be retained as the starting point for new adaptations. Users should save a copy of the working files before making changes and record any hardware, software, or calibration modifications.
S2. Hardware Configurations and Wiring
The framework was implemented using a Raspberry Pi Pico W and three additional sensor configurations. The required connections depend on the selected sensor, the particular module board, and the pin assignments used in the corresponding source code.
Table S1. Hardware configurations and required connection information.
Configuration	Measurement	Connection information to provide
Raspberry Pi Pico W template	Onboard temperature	Pico W board and MicroPython configuration
HC-SR04	Distance	Trigger pin, echo pin, power supply, and ground
HX711 with load cell	Force	HX711 data and clock pins, load-cell connections, power supply, and ground
BH1750	Light intensity	I²C data and clock pins, power supply, and ground
For reproduction, use the pin assignments documented in the corresponding repository program and wiring diagram. Do not assume that the pin assignments are interchangeable between modules.
Before powering the circuit, check the module supply requirements and the Pico W input-voltage limits. In particular, verify that the HC-SR04 echo signal is compatible with the Pico W's 3.3-V GPIO inputs; use appropriate level conversion when required by the specific module. Confirm the load-cell wiring and the HX711 connections before applying loads. For the BH1750, verify the I²C connections and address configuration where applicable.
Table S2. Wiring record for the published implementation.
Connection	Pin or terminal used
HC-SR04 trigger	GPIO pin 16
HC-SR04 echo	GPIO pin 17
HX711 data	GPIO pin 16
HX711 clock	GPIO pin 17
BH1750 SDA	GPIO pin 16
BH1750 SCL	GPIO pin 17
BH1750 ADDR	GPIO pin GND
Sensor power	GPIO pin 36
Sensor ground	GPIO pin 38
The table above should be completed from the actual working circuits and repository code before publication. The published wiring diagrams and source files should agree with one another.
S3. Reusable AI Adaptation Prompt
The following prompt template can be used to request a sensor-specific adaptation of the existing framework. Replace the bracketed fields with information for the new sensor and investigation.
Prompt template
I am adapting an existing Raspberry Pi Pico W digital-physics laboratory for a new sensor. Please modify the existing software rather than creating an unrelated standalone program.
Existing files
•	main.py: [Describe the current acquisition and processing structure.]
•	index.html: [Describe the existing display, graphing, and data-export functions.]
New sensor
•	Sensor model: [Insert model]
•	Physical quantity: [Insert quantity and SI unit]
•	Communication protocol: [Insert protocol]
•	Pin assignments and wiring: [Insert verified information]
•	Power and logic-level requirements: [Insert specifications]
•	Measurement range and resolution: [Insert specifications]
•	Calibration or conversion procedure: [Insert known procedure or reference]
•	Required sampling interval: [Insert requirement]
•	Required graph and data-export format: [Insert requirements]
Please:
1.	Identify the sensor-specific functions that need to be added or modified.
2.	Preserve the shared data-handling structure and existing interface wherever possible.
3.	Provide code compatible with the specified MicroPython environment and hardware.
4.	Explain sensor initialization, data acquisition, error handling, calibration, and conversion to the required physical quantity.
5.	State the units and assumptions used in the calculations.
6.	Identify any missing hardware information or uncertain assumptions instead of inventing specifications.
7.	Provide wiring instructions consistent with the proposed code.
8.	Explain how to test the module and identify plausible failure modes.
9.	List all files that must be changed and provide the complete revised contents of those files when needed.
Do not claim that the module has been tested on physical hardware unless such testing has actually been performed. Distinguish code-generation suggestions from verified results.
S4. Deployment and Verification Checklist
AI-generated code should be treated as a candidate implementation, not as evidence that an experiment is ready for classroom use. The teacher should complete the following checks after adapting a module.
Table S3. Verification checklist.
Check	Verification action	Record
Hardware connections	Compare wiring with the sensor specifications and program pin assignments	[Pass / Revise]
Electrical compatibility	Verify supply voltage and signal-level requirements	[Pass / Revise]
Communication	Confirm that the sensor initializes and returns readings without communication errors	[Pass / Revise]
Data acquisition	Check that readings update and that failures or invalid readings are handled appropriately	[Pass / Revise]
Physical quantity	Confirm that raw sensor values are converted to the intended quantity	[Pass / Revise]
Units	Check displayed labels, graph axes, and exported data units	[Pass / Revise]
Calibration	Apply the documented calibration procedure or compare with an appropriate reference	[Pass / Revise / Not assessed]
Measurement range	Check whether readings remain within the intended operating range	[Pass / Revise]
Physical plausibility	Vary the experimental conditions and determine whether the readings respond as expected	[Pass / Revise]
Visualization	Confirm that the displayed values and graph correspond to the acquired data	[Pass / Revise]
Data export	Export a data file and check its contents and units	[Pass / Revise]
Classroom readiness	Confirm that limitations, operating instructions, and relevant safety precautions are documented	[Pass / Revise]
If a check fails, the teacher should identify whether the problem arises from the wiring, sensor configuration, acquisition code, calibration, conversion, or interface. Revise the relevant component and repeat the affected checks. A module should not be used for quantitative classroom investigations until the checks relevant to its intended use have been completed.
A successful response to changing experimental conditions establishes basic functionality, but does not by itself establish measurement accuracy. Quantitative accuracy claims require comparison with an appropriate reference or another documented validation procedure.

