package com.autoimmune.orders;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.stereotype.Component;

/** Reports DOWN when the order catalog is switched off in configuration. */
@Component
public class CatalogHealthIndicator implements HealthIndicator {

    private final boolean enabled;

    public CatalogHealthIndicator(@Value("${orders.catalog.enabled:false}") boolean enabled) {
        this.enabled = enabled;
    }

    @Override
    public Health health() {
        if (enabled) {
            return Health.up().withDetail("catalog", "enabled").build();
        }
        return Health.down().withDetail("catalog", "disabled").build();
    }
}
