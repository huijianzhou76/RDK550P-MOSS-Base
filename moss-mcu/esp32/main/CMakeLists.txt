idf_component_register(
    SRCS
        "main.cpp"
        "motion_controller.cpp"
        "peripherals.cpp"
        "ir_service.cpp"
    INCLUDE_DIRS "."
    REQUIRES driver json esp_timer
)
